"""Wire everything together from a config dict. The only module that knows all the parts of the core, and the one that finds and sets up the packs."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from . import metrics, obs
from .audit import AuditChain
from .history import DecisionHistory
from .agents.service import AgentService
from .agents.store import AgentLog
from .api.hardening import MIN_KEY_LENGTH, access_key_warning, demo_key_problem, weak_key
from .approvals.channels.base import Channel
from .approvals.channels.registry import build_channels
from .approvals.models import Recipient
from .approvals.router import ApprovalRouter
from .approvals.service import ApprovalService
from .auth.erpnext import ErpNextLogin
from .auth.sessions import LoginThrottle, SessionStore
from .common import Actor, Clock, utcnow
from .config import load_config
from .erp.base import ERPClient
from .erp.erpnext import ERPNextClient
from .erp.mock import MockERP
from .packs import PackError, PackRegistry
from .settings_store import SettingsStore
from .storage import Database
from .toolkit import Toolkit
from .workorders import WorkOrderQueue

log = logging.getLogger(__name__)


class StartupRefused(ValueError):
    """The configuration is one the program will not start with. The text says why in plain words; the command line prints it without a traceback."""


@dataclass
class App:
    """The running application: the core's parts, and the services the packs add (`services`, also reachable as attributes: `app.inventory`, `app.procurement` ...)."""

    config: dict[str, Any]
    db: Database
    settings: SettingsStore
    erp: ERPClient
    channels: dict[str, Channel]
    router: ApprovalRouter
    approvals: ApprovalService
    workorders: WorkOrderQueue
    tokens: dict[str, Actor]
    clock: Clock
    packs: PackRegistry
    login: Optional[ErpNextLogin] = None            # sign-in through ERPNext (None for the demo ERP)
    sessions: Optional[SessionStore] = None
    throttle: Optional[LoginThrottle] = None
    conversation: Optional[Any] = None              # typing to the assistant from Telegram / WhatsApp: set by the pack that handles conversations
    toolkit: Optional[Any] = None                   # the tool registry (toolkit.py): what an MCP client or an agent may ask of the application
    agent_log: Optional[Any] = None                 # the record of what the agents did
    agents: Optional[Any] = None                    # the AI agents (agents/service.py); the optional 'agents' dependencies are only needed when one runs
    services: dict[str, Any] = field(default_factory=dict)      # what the packs that are switched on provide, by name
    reschedule_scopes: tuple[str, ...] = ()        # settings sections whose change reschedules the packs' jobs (besides system and agents)
    audit: Optional[Any] = None                     # the seal over the decision history (audit.py)
    history: Optional[Any] = None                   # "Why did you do that?": every past decision with its reasoning (history.py)
    observability: Optional[Any] = None             # the `observability:` section (obs.py ObsSettings): log format, metrics, tracing

    def __getattr__(self, name: str) -> Any:
        # reached only when the application itself has no such attribute: a service a pack provided
        services = self.__dict__.get("services")
        if services is not None and name in services:
            return services[name]
        raise AttributeError(f"the application has no {name!r}: no pack that is switched on provides it")


def build_erp(cfg: dict[str, Any], clock: Clock) -> ERPClient:
    kind = cfg.get("type", "mock")
    if kind == "mock":
        return MockERP(today=clock().date(), seed=int(cfg.get("seed", 7)))
    if kind == "erpnext":
        for key in ("url", "api_key", "api_secret"):
            if not cfg.get(key):
                raise ValueError(f"erp.{key} is required for erp.type=erpnext (set it in settings.yaml or the environment)")
        return ERPNextClient(
            cfg["url"], cfg["api_key"], cfg["api_secret"],
            warehouses=cfg.get("warehouses"), consumption_voucher_types=cfg.get("consumption_voucher_types"),
            item_groups=cfg.get("item_groups"), company=cfg.get("company"),
        )
    raise ValueError(f"Unknown erp.type {kind!r} (use 'mock' or 'erpnext')")


def build_app(
    config: Optional[dict[str, Any]] = None,
    config_path: Optional[str] = None,
    clock: Clock = utcnow,
    erp: Optional[ERPClient] = None,
    channels: Optional[dict[str, Channel]] = None,
    http: Any = None,
    db_path: Optional[str] = None,
    login: Optional[ErpNextLogin] = None,
) -> App:
    cfg = config if config is not None else load_config(config_path)
    problem = demo_key_problem(cfg)             # before anything is created or opened
    if problem:
        raise StartupRefused(problem)
    try:
        observability = obs.settings_from(cfg)
    except ValueError as exc:
        raise StartupRefused(str(exc)) from None
    obs.register_secrets_from_config(cfg)       # so that no log line, in any format, can carry a key, a token or a phone number that this configuration holds

    path = db_path or cfg["database"]["path"]
    if path != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    db = Database(path)

    settings = SettingsStore(db, base_system=cfg.get("system") or {}, clock=clock, base_assistant=cfg.get("assistant") or {}, base_agents=cfg.get("agents") or {},
                             base_packs=cfg.get("packs") or {})
    packs = PackRegistry.discover()
    packs.bind_settings(settings.packs)
    erp = erp or build_erp(cfg["erp"], clock)
    channels = channels if channels is not None else build_channels(cfg.get("channels") or {}, http=http)

    recipients = [Recipient.model_validate(r) for r in (cfg.get("approvals") or {}).get("recipients") or []]
    for r in recipients:
        for rc in r.channels:
            if rc.type not in channels:
                log.warning("recipient %s lists channel %r which is not enabled - it will be skipped", r.id, rc.type)
            else:
                problem = channels[rc.type].address_problem(rc.address)
                if problem:
                    log.warning("recipient %s on %s: the address %s - nothing will be sent to them there", r.id, rc.type, problem)
    router = ApprovalRouter(channels, recipients, (cfg.get("approvals") or {}).get("strategy", "all"))
    approvals = ApprovalService(db, router, system_settings=settings.system, clock=clock)
    workorders = WorkOrderQueue(db, clock)

    tokens: dict[str, Actor] = {}
    for t in (cfg.get("auth") or {}).get("tokens") or []:
        if not t.get("token"):
            log.warning("auth token for %s is empty (is the environment variable set?) - skipped", t.get("user"))
            continue
        tokens[t["token"]] = Actor(id=t["user"], roles=frozenset(t.get("roles", ["viewer"])), via="api")
        if weak_key(t["token"], int((cfg.get("auth") or {}).get("min_key_length", MIN_KEY_LENGTH))):
            log.warning("access key for %s is short or well known: it is accepted only from this machine, never through a tunnel or proxy. "
                        "Set a long random one (at least %d characters) in .env to use it remotely.", t.get("user"), MIN_KEY_LENGTH)

    auth_cfg = cfg.get("auth") or {}
    warning = access_key_warning(auth_cfg, bool(tokens))
    if warning:
        log.warning(warning)
    if login is None and isinstance(erp, ERPNextClient) and auth_cfg.get("erpnext_login", True):
        wanted = packs.erp_capabilities()                       # what a person needs in ERPNext to look and to decide: from the packs that are switched on
        login = ErpNextLogin(erp.base_url, look=tuple(d for d, p in wanted["look"] if p == "read"), decide=tuple(d for d, p in wanted["decide"] if p == "create"))
    sessions = SessionStore(login, ttl_seconds=float(auth_cfg.get("session_hours", 12)) * 3600,
                            recheck_seconds=float(auth_cfg.get("recheck_minutes", 15)) * 60) if login else None
    app = App(cfg, db, settings, erp, channels, router, approvals, workorders, tokens, clock, packs,
              login=login, sessions=sessions, throttle=LoginThrottle() if login else None, observability=observability)
    app.agent_log = AgentLog(db, clock)
    app.toolkit = Toolkit(app, log_call=app.agent_log.log_tool)
    app.toolkit.is_enabled = packs.is_enabled      # a pack that is switched off answers nothing, at once
    app.agents = AgentService(app)                 # the optional 'mcp' package is only needed when an agent actually runs
    app.audit = AuditChain(db, clock)
    app.history = DecisionHistory(app)
    approvals.on_closed.append(app.audit.sweep)    # each answer, expiry or cancellation is sealed at once
    settings.on_change(app.audit.sweep)            # and so is each change of settings
    _set_up_packs(app)
    settings.ignore_unknown_system = any(not packs.is_enabled(n) for n, m in packs.manifests.items() if m.status != "skeleton")
    settings.validate()                            # the values in the settings file are checked once every pack has said which fields it adds
    try:
        app.audit.sweep()                          # what was closed before (or while the application was stopped) is sealed now
    except Exception:  # noqa: BLE001 - a seal that cannot be written must not stop the application; `history --verify` reports what is not sealed
        log.exception("could not seal the decision history")
    metrics.attach(app)                            # recording and /metrics on or off, as `observability.metrics` says (off by default)
    return app


def _set_up_packs(app: App) -> None:
    """Load and set up every pack that is switched on, then check that what it registered is what its manifest declares. A broken pack stops the application: a half-started
    one would answer some questions and not others."""
    registry = app.packs
    for name in registry.unknown_in_settings():
        log.warning("the settings switch a pack called %r, but no such pack is installed", name)
    for name in registry.enabled_names():
        if registry.manifests[name].status == "skeleton":
            log.info("pack %s is a skeleton: nothing to load", name)
            continue
        try:
            pack = registry.load(name)
            pack.setup(app)
            registry.check_registered(name, app.toolkit.registered_for(name))
        except PackError:
            raise
        except Exception as exc:
            raise PackError(f"pack {name!r} could not be set up: {type(exc).__name__}: {str(exc)[:200]}") from exc
        log.info("pack %s %s is on", name, registry.manifests[name].version)
