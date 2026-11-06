"""The channel-independent approval workflow: one decision, safe retries, multi-channel delivery."""
import pytest

from ai_munimji_engine.core.approvals.channels.base import Channel
from ai_munimji_engine.core.approvals.models import ApprovalOption, ApprovalRequest, DeliveryResult, Recipient, RecipientChannel, Section
from ai_munimji_engine.core.approvals.router import ApprovalRouter
from ai_munimji_engine.core.approvals.service import ApprovalService, KindHandler
from erp_agents.stores import build_core_stores

from conftest import APPROVER, OWNER, VIEWER, storage_kind


class RecordingChannel(Channel):
    def __init__(self, type_, fail=0):
        self.type = type_
        self.fail = fail
        self.sent, self.texts, self.outcomes, self.reminders = [], [], [], []

    def send_approval(self, record, address):
        if self.fail:
            self.fail -= 1
            return DeliveryResult(ok=False, error="boom")
        self.sent.append((record.id, address))
        return DeliveryResult(ok=True, external_id=f"{self.type}-{len(self.sent)}")

    def send_text(self, address, text):
        self.texts.append(text)
        return DeliveryResult(ok=True)

    def send_reminder(self, record, address):
        self.reminders.append(record.id)
        return DeliveryResult(ok=True)

    def update_outcome(self, record, external_id, address, text):
        self.outcomes.append((record.id, external_id, text))


def request(kind="test.thing"):
    return ApprovalRequest(
        kind=kind, title="Do the thing", headline="Please decide", severity="HIGH",
        sections=[Section(heading="Why", lines=["because"])],
        details=[Section(heading="More", lines=["numbers"])],
        options=[
            ApprovalOption(action="yes", label="Yes", style="primary"),
            ApprovalOption(action="no", label="No"),
            ApprovalOption(action="details", label="Details", terminal=False),
        ],
    )


def service(clock, channels, recipients, strategy="all"):
    stores = build_core_stores(storage_kind())
    router = ApprovalRouter(channels, recipients, strategy)
    return ApprovalService(stores.approvals, router, clock=clock, inbound=stores.inbound), stores


def one_recipient(*types):
    return [Recipient(id="owner", name="Owner", roles=["admin", "approver"],
                      channels=[RecipientChannel(type=t, address=f"{t}-addr") for t in types])]


def test_one_decision_wins_and_repeat_presses_are_harmless(clock):
    effects = []
    svc, _ = service(clock, {"a": RecordingChannel("a")}, one_recipient("a"))
    svc.register_handler("test.thing", KindHandler(on_decision=lambda rec, d: effects.append(d.action)))
    rec = svc.submit(request())
    first = svc.decide(rec.id, "yes", OWNER, "a")
    second = svc.decide(rec.id, "no", APPROVER, "b")           # same request answered on another channel
    assert first.status == "decided" and second.status == "already_decided"
    assert "Already answered" in second.message and "Yes" in second.message and effects == ["yes"]
    assert svc.get(rec.id).decision.action == "yes"


def test_permissions_and_validation(clock):
    svc, _ = service(clock, {"a": RecordingChannel("a")}, one_recipient("a"))
    rec = svc.submit(request())
    assert svc.decide(rec.id, "yes", VIEWER, "a").status == "forbidden"
    assert svc.decide(rec.id, "maybe", OWNER, "a").status == "invalid_action"
    assert svc.decide("nope", "yes", OWNER, "a").status == "not_found"
    assert svc.get(rec.id).status == "pending"


def test_details_is_information_only(clock):
    svc, _ = service(clock, {"a": RecordingChannel("a")}, one_recipient("a"))
    rec = svc.submit(request())
    res = svc.decide(rec.id, "details", OWNER, "a")
    assert res.status == "info" and res.info_sections[0].heading == "More"
    assert svc.get(rec.id).status == "pending"


def test_cannot_decide_after_expiry(clock):
    svc, _ = service(clock, {"a": RecordingChannel("a")}, one_recipient("a"))
    rec = svc.submit(request(), expiry_hours=2)
    clock.advance(hours=3)
    res = svc.decide(rec.id, "yes", OWNER, "a")
    assert res.status == "expired" and svc.get(rec.id).status == "expired"


def test_handler_failure_rolls_the_decision_back(clock):
    def boom(rec, d):
        raise RuntimeError("database says no")
    svc, _ = service(clock, {"a": RecordingChannel("a")}, one_recipient("a"))
    svc.register_handler("test.thing", KindHandler(on_decision=boom))
    rec = svc.submit(request())
    res = svc.decide(rec.id, "yes", OWNER, "a")
    assert res.status == "error" and "Nothing was changed" in res.message
    assert svc.get(rec.id).status == "pending"                 # user can simply try again


def test_every_channel_receives_the_request_and_is_told_the_outcome(clock):
    a, b = RecordingChannel("a"), RecordingChannel("b")
    svc, db = service(clock, {"a": a, "b": b}, one_recipient("a", "b"))
    rec = svc.submit(request())
    assert len(a.sent) == len(b.sent) == 1
    svc.decide(rec.id, "yes", OWNER, "a")
    assert a.outcomes and b.outcomes and "Yes" in a.outcomes[0][2]     # buttons on the other channel get retired


def test_failover_stops_at_first_channel_that_works(clock):
    a, b = RecordingChannel("a", fail=1), RecordingChannel("b")
    svc, db = service(clock, {"a": a, "b": b}, one_recipient("a", "b"), strategy="failover")
    svc.submit(request())
    assert a.sent == [] and len(b.sent) == 1
    rows = svc.store.deliveries(svc.list()[0].id)
    assert [(r["channel"], r["status"]) for r in rows] == [("a", "failed"), ("b", "sent")]


def test_failed_delivery_is_retried_later(clock):
    a = RecordingChannel("a", fail=1)
    svc, db = service(clock, {"a": a}, one_recipient("a"))
    svc.submit(request())
    assert a.sent == []
    assert svc.retry_failed_deliveries() == 1 and len(a.sent) == 1
    assert svc.retry_failed_deliveries() == 0                  # nothing left to retry


def test_retries_give_up_eventually(clock):
    a = RecordingChannel("a", fail=99)
    svc, db = service(clock, {"a": a}, one_recipient("a"))
    svc.submit(request())
    for _ in range(10):
        svc.retry_failed_deliveries()
    assert svc.store.deliveries(svc.list()[0].id)[0]["status"] == "given_up"


def test_recipients_without_approver_role_are_not_asked(clock):
    a = RecordingChannel("a")
    recips = one_recipient("a") + [Recipient(id="viewer", name="V", roles=["viewer"], channels=[RecipientChannel(type="a", address="v")])]
    svc, _ = service(clock, {"a": a}, recips)
    svc.submit(request())
    assert [addr for _, addr in a.sent] == ["a-addr"]


def test_router_maps_senders_to_people_and_ignores_strangers():
    r = ApprovalRouter({"whatsapp": RecordingChannel("whatsapp")},
                       [Recipient(id="owner", name="O", roles=["approver"],
                                  channels=[RecipientChannel(type="whatsapp", address="+91 98000 00000")])])
    assert r.recipient_for("whatsapp", "919800000000").id == "owner"     # formatting differences ignored
    assert r.recipient_for("whatsapp", "919811111111") is None
    assert r.recipient_for("telegram", "919800000000") is None           # wrong channel type


def test_cancel_only_affects_pending(clock):
    svc, _ = service(clock, {"a": RecordingChannel("a")}, one_recipient("a"))
    rec = svc.submit(request())
    assert svc.cancel(rec.id, "no longer needed") is True
    assert svc.cancel(rec.id, "again") is False
    assert svc.decide(rec.id, "yes", OWNER, "a").status == "cancelled"
