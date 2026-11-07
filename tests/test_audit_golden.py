"""The seal over one known history is the same chain on every backend.

`ai_munimji_engine.testkit.stores.audit_history` makes a fixed history; `AUDIT_GOLDEN` is the seal it gives, worked out once on SQLite. SQLite and memory must give it here, and the
Frappe app (AI-Munimji, tests/test_audit_seal.py) must give it on DocTypes: the same hashes, in the same order, for the same events.
"""

from ai_munimji_engine.testkit import stores as k


def test_the_seal_over_the_known_history_is_the_golden_one(stores):
    seen = k.audit_seen(stores)
    assert seen["ok"] is True
    assert {key: seen[key] for key in k.AUDIT_GOLDEN} == k.AUDIT_GOLDEN


def test_a_request_closed_after_the_sweep_is_counted_as_not_sealed_yet(stores):
    chain = k.audit_history(stores)
    chain.sweep()
    stores.approvals.decide_if_pending("a3", '{"action":"reject"}', k.T3)
    verdict = chain.verify()
    assert verdict["ok"] is True and verdict["not_sealed_yet"] == 1
