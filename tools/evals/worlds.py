"""The world a case is asked in: the demo ERP (MockERP, deterministic), the application built on it exactly as the tests build it, and the few things a case may add on top
(an open order, a goods receipt, a past decision ...). Every case gets a fresh application, so one case can never change the answer to another."""
from __future__ import annotations

import copy
import re
from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional

from erp_agents.core.bootstrap import build_app
from erp_agents.core.common import Actor
from erp_agents.core.config import DEFAULT_CONFIG
from erp_agents.core.erp.base import CountLine, ReceiptLine
from erp_agents.core.erp.mock import MockERP

from .cases import Case

OWNER = Actor("eval-owner", frozenset({"admin", "approver"}), "eval")
APPROVER = Actor("eval-approver", frozenset({"approver"}), "eval")
VIEWER = Actor("eval-viewer", frozenset({"viewer"}), "eval")
DECIDER = Actor("eval-priya", frozenset({"admin", "approver"}), "eval")          # whoever answered the requests in a case's past (the history names nobody to the AI)
WORLDS = ("demo", "quiet")
START = datetime(2026, 10, 16, 8, 0, tzinfo=timezone.utc)


class FakeClock:
    def __init__(self) -> None:
        self.now = START

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kw: float) -> None:
        self.now += timedelta(**kw)


class World:
    def __init__(self, app: Any, clock: FakeClock) -> None:
        self.app, self.clock = app, clock

    @property
    def today(self) -> date:
        return self.app.inventory.today()


def make_app(erp: Any, clock: FakeClock) -> Any:
    """The application as tests/conftest.py builds it: an in-memory database, the demo ERP, no channel that could reach anybody, messages handled on the spot."""
    base = copy.deepcopy(DEFAULT_CONFIG)
    base["database"]["path"] = ":memory:"
    base["conversation"] = {"synchronous": True}
    base.setdefault("auth", {})["min_key_length"] = 1
    base["auth"]["access_keys"] = "anywhere"
    base.setdefault("system", {})["stock_alerts"] = False
    app = build_app(config=base, clock=clock, erp=erp, channels=None, http=None, db_path=":memory:")
    console = app.channels.get("console")
    if console is not None:
        console.writer = lambda s: None
    return app


def build_world(case: Case) -> World:
    if case.world not in WORLDS:
        raise ValueError(f"{case.id}: unknown world {case.world!r} (known: {', '.join(WORLDS)})")
    clock = FakeClock()
    app = make_app(MockERP(today=clock().date()), clock)
    if case.world == "demo":
        app.inventory.run()                                       # what the product does every morning: alerts for what is short, waiting for a person's answer
    world = World(app, clock)
    for op in case.setup:
        (name, arg), = op.items()
        apply_op(world, name, arg)
    return world


# ------------------------------------------------------------------------------------------------------------------- the few things a case may add
def apply_op(world: World, name: str, arg: Any) -> None:
    app, erp, today = world.app, world.app.erp, world.today
    if name == "run":
        {"inventory": app.inventory.run, "procurement": app.procurement.run}[arg]()
    elif name == "item":
        per_day = float(arg.get("per_day", 5))
        erp.add_item(arg["code"], arg["name"], lambda age, v=per_day: v, float(arg.get("on_hand", 100)), group=arg.get("group", "General"), uom=arg.get("uom", "pcs"),
                     lead_time_days=arg.get("lead_days", 7), warehouses=arg.get("warehouses"), scenario="eval")
    elif name == "stock":
        fields = {k: float(v) for k, v in arg.items() if k != "item"}
        erp.set_stock(arg["item"], **fields)
    elif name == "open_order":
        erp.add_open_order(arg["name"], arg.get("supplier", "SUP-PRIME"), arg["item"], float(arg["qty"]), today + timedelta(days=int(arg["due_in_days"])),
                           received=float(arg.get("received", 0)), supplier_name=arg.get("supplier_name", "Prime Supplies"))
    elif name == "receipt":
        item = arg.get("item", "BOLT-M8")
        info = erp.items[item].info
        received, rejected = float(arg["received"]), float(arg.get("rejected", 0))
        erp.receipts.append(ReceiptLine(receipt=arg["name"], posting_date=today - timedelta(days=2), supplier="SUP-PRIME", supplier_name="Prime Supplies", item_code=item,
                                        item_name=info.name, uom=info.uom, received_qty=received, accepted_qty=received - rejected, rejected_qty=rejected,
                                        rate=float(arg.get("rate", 10.0)), purchase_order=arg.get("purchase_order", "PO-0009"), po_qty=float(arg.get("po_qty", 100.0)), po_rate=10.0,
                                        po_due=today - timedelta(days=3), po_received_before=float(arg.get("before", 0))))
    elif name == "alternative":
        erp.alternatives.setdefault(arg["item"], []).append(arg["alt"])
    elif name == "count":
        erp.counts.append(CountLine(arg.get("name", "SR-0001"), today - timedelta(days=int(arg.get("days_ago", 1))), arg["item"], arg.get("warehouse", "Main Warehouse"),
                                    float(arg["counted"]), float(arg["expected"])))
    elif name == "decide":
        record = find_approval(app, arg["kind"], arg["item"], status="pending")
        if record is None:
            raise ValueError(f"setup decide: no pending {arg['kind']} request for {arg['item']}")
        app.approvals.decide(record.id, arg["action"], DECIDER, "app", arg.get("params"))
    elif name == "order_request":
        res = app.toolkit.call("prepare_order_request", {"item_code": arg["item"], "qty": arg.get("qty"), "reason": "eval setup"}, APPROVER, via="eval")
        if not res.get("request_id"):
            raise ValueError(f"setup order_request for {arg['item']}: {res}")
    elif name == "setting":
        app.settings.update_section(OWNER, arg["section"], dict(arg["set"]))
    else:  # pragma: no cover - cases.py already refuses unknown steps
        raise ValueError(f"unknown setup step {name!r}")


def find_approval(app: Any, kind: str, item: str, status: Optional[str] = None) -> Optional[Any]:
    for rec in app.approvals.list(status, 1000):
        if rec.kind == kind and (rec.request.facts or {}).get("item_code") == item:
            return rec
    return None


# ------------------------------------------------------------------------------------------------------------------- numbers a script may read from the world
_PLACEHOLDER = re.compile(r"\$\{(\w+)\(([^)]*)\)\.([\w.\-]+)\}")


def resolve(app: Any, obj: Any) -> Any:
    """Replace ${tool(arg=value, ...).path} with that field of what the tool returns, anywhere in a case's script and expectations. A script written this way survives a change
    in the demo data; a literal number pins it. The tool is called as a viewer and logged as via='eval', which the observation ignores."""
    if isinstance(obj, str):
        return _PLACEHOLDER.sub(lambda m: _lookup(app, m), obj)
    if isinstance(obj, list):
        return [resolve(app, x) for x in obj]
    if isinstance(obj, dict):
        return {k: resolve(app, v) for k, v in obj.items()}
    return obj


def _lookup(app: Any, m: "re.Match[str]") -> str:
    name, raw_args, path = m.group(1), m.group(2), m.group(3)
    args: dict[str, Any] = {}
    for part in [p.strip() for p in raw_args.split(",") if p.strip()]:
        key, _, val = part.partition("=")
        args[key.strip()] = float(val) if re.fullmatch(r"-?\d+(\.\d+)?", val.strip()) else val.strip()
    result = app.toolkit.call(name, args, VIEWER, via="eval")
    if "error" in result:
        raise ValueError(f"{m.group(0)}: the tool said {result['error']}")
    cur: Any = result
    for part in path.split("."):
        cur = cur[int(part)] if isinstance(cur, list) else cur[part]
    if isinstance(cur, float):
        return str(int(cur)) if cur == int(cur) else f"{cur:.10g}"
    return str(cur)
