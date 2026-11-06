"""Alert persistence + lifecycle for the inventory agent."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional

from ai_munimji_engine.storage import dumps, loads
from ai_munimji_engine.stores.stock.alerts import AlertStore

from ai_munimji_engine.core.common import Clock, iso, parse_dt, short_id, utcnow

ACTIVE = ("awaiting",)
OPEN_STATES = ("awaiting", "snoozed", "dismissed", "actioned")


@dataclass
class Alert:
    id: str
    item_code: str
    kind: str
    severity: int
    status: str
    created_at: datetime
    updated_at: datetime
    analysis: dict[str, Any]
    reasoning_text: str
    approval_id: Optional[str]
    snoozed_until: Optional[datetime]
    suppress_until: Optional[datetime]
    cooldown_until: Optional[datetime]
    status_note: str
    run_id: Optional[str]


class AlertRepo:
    def __init__(self, store: AlertStore, clock: Clock = utcnow) -> None:
        self.store = store
        self.clock = clock

    def create(self, item_code: str, kind: str, severity: int, analysis: dict, reasoning_text: str, run_id: Optional[str]) -> Alert:
        now = iso(self.clock())
        alert_id = short_id(10)
        self.store.insert(alert_id, item_code, kind, severity, now, dumps(analysis), reasoning_text, run_id)
        return self.get(alert_id)

    def get(self, alert_id: str) -> Optional[Alert]:
        r = self.store.get(alert_id)
        return _to_alert(r) if r else None

    def latest_for_item(self, item_code: str) -> Optional[Alert]:
        r = self.store.latest_for_item(item_code)
        return _to_alert(r) if r else None

    def open_for_item(self, item_code: str) -> list[Alert]:
        return [_to_alert(r) for r in self.store.open_for_item(item_code, OPEN_STATES)]

    def list(self, status: Optional[str] = None, item_code: Optional[str] = None, limit: int = 100) -> list[Alert]:
        return [_to_alert(r) for r in self.store.list(status, item_code, limit)]

    def set_approval(self, alert_id: str, approval_id: str) -> None:
        self.store.set_approval(alert_id, approval_id, iso(self.clock()))

    def set_reasoning(self, alert_id: str, text: str) -> None:
        self.store.set_reasoning(alert_id, text)

    def update_status(self, alert_id: str, status: str, note: str = "", *, snoozed_until: Optional[datetime] = None,
                      suppress_until: Optional[datetime] = None, cooldown_until: Optional[datetime] = None) -> None:
        self.store.update_status(alert_id, status, note, iso(self.clock()), iso(snoozed_until), iso(suppress_until), iso(cooldown_until))

    def by_approval(self, approval_id: str) -> Optional[Alert]:
        r = self.store.by_approval(approval_id)
        return _to_alert(r) if r else None


def _to_alert(r) -> Alert:
    return Alert(
        id=r["id"], item_code=r["item_code"], kind=r["kind"], severity=r["severity"], status=r["status"],
        created_at=parse_dt(r["created_at"]), updated_at=parse_dt(r["updated_at"]),
        analysis=loads(r["analysis_json"]), reasoning_text=r["reasoning_text"], approval_id=r["approval_id"],
        snoozed_until=parse_dt(r["snoozed_until"]), suppress_until=parse_dt(r["suppress_until"]),
        cooldown_until=parse_dt(r["cooldown_until"]), status_note=r["status_note"] or "", run_id=r["run_id"],
    )
