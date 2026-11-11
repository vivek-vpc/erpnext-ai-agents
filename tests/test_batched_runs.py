"""A long stock check works through the items in batches and reports at the end of each one (finding A1 of the production-readiness review).

The Frappe app commits there, so that a run is no longer one transaction for the whole site: the rows it wrote are not locked until it ends, a failure near the end keeps what the
batches before it did, and its lease can be renewed. For that to be safe the batched run has to do exactly what the unbatched one does, and a run that stops half way and is run
again has to act on each item once."""
import pytest

from conftest import OWNER
from erp_agents.core.erp.mock import MockERP

KEYS = ("checked", "ok", "early_warnings", "breaches", "new_alerts", "skipped", "resolved", "new_notices")


def requests_of(app):
    """What a person would be asked: (kind, item) of every request that is waiting."""
    return sorted((r.kind, r.request.facts.get("item_code")) for r in app.approvals.list("pending", 1000))


def what_was_done(report):
    return sorted((a["item_code"], a["action"]) for a in report.actions)


@pytest.fixture
def two(make_app, clock):
    """Two applications on the same demo ERP data: one to run in one piece, one to run in batches."""
    return make_app(erp=MockERP(today=clock().date())), make_app(erp=MockERP(today=clock().date()))


def test_a_run_in_batches_does_what_a_run_in_one_piece_does(two):
    whole, cut = two
    one = whole.inventory.run()
    many = cut.inventory.run(batch_size=3, checkpoint=lambda *_: None)
    assert one.new_alerts > 3, "the demo data should call for several alerts, or this proves nothing"
    assert {k: getattr(many, k) for k in KEYS} == {k: getattr(one, k) for k in KEYS}
    assert what_was_done(many) == what_was_done(one)
    assert requests_of(cut) == requests_of(whole)
    assert many.errors == one.errors


def test_the_checkpoint_is_called_at_the_end_of_every_batch_and_after_the_last_item(make_app, clock):
    app = make_app(erp=MockERP(today=clock().date()))
    calls = []
    report = app.inventory.run(batch_size=4, checkpoint=lambda stage, done, total: calls.append((stage, done, total)))
    planning = [c for c in calls if c[0] == "planning"]
    reading = [c for c in calls if c[0] == "checking"]
    raising = [c for c in calls if c[0] == "raising"]
    total = report.checked
    assert [c[1] for c in planning] == [*range(4, total, 4), total], "working out each item's settings is reported too: it can take minutes, and the caller's lease must be kept"
    assert [c[1] for c in reading] == [*range(4, total, 4), total], "one call per batch of four, and one for the last, smaller one"
    assert all(c[2] == total for c in reading + raising)
    assert [c[1] for c in raising] == [*range(4, total, 4), total]
    assert calls == planning + reading + raising, "all the planning and reading is done before anything is raised: it writes nothing, so it holds no lock"


def test_without_a_batch_size_the_checkpoint_is_never_called(make_app, clock):
    app = make_app(erp=MockERP(today=clock().date()))
    calls = []
    app.inventory.run(checkpoint=lambda *a: calls.append(a))
    assert calls == []


def test_a_run_that_stops_in_the_middle_and_is_run_again_makes_each_request_once(two):
    whole, broken = two
    whole.inventory.run()

    def stop_after_the_first_batch_of_raising(stage, done, total):
        if stage == "raising":
            raise RuntimeError("the worker was killed")

    with pytest.raises(RuntimeError, match="killed"):
        broken.inventory.run(batch_size=3, checkpoint=stop_after_the_first_batch_of_raising)
    half = requests_of(broken)
    assert 0 < len(half) < len(requests_of(whole)), "the first batches raised their requests and the run stopped before the others"

    again = broken.inventory.run(batch_size=3, checkpoint=lambda *_: None)
    assert requests_of(broken) == requests_of(whole), "the same requests as one uninterrupted run, none twice"
    assert len(requests_of(broken)) == len(set(requests_of(broken)))
    assert any(a["action"] == "skipped:pending_approval" for a in again.actions), "what the first run raised was found, not raised again"
    assert again.new_alerts == len(requests_of(whole)) - len(half)


def test_a_checkpoint_that_raises_while_reading_leaves_nothing_behind(make_app, clock):
    app = make_app(erp=MockERP(today=clock().date()))

    def stop_while_reading(stage, done, total):
        if stage == "checking":
            raise RuntimeError("lease lost")

    with pytest.raises(RuntimeError, match="lease lost"):
        app.inventory.run(batch_size=3, checkpoint=stop_while_reading)
    assert requests_of(app) == [], "reading writes nothing, so stopping while reading changes nothing"


def test_the_procurement_run_reports_at_the_end_of_every_batch_too(make_app, clock):
    app = make_app(erp=MockERP(today=clock().date()))
    app.inventory.run()
    reorders = [r for r in app.approvals.list("pending", 100) if r.kind == "inventory.reorder"]
    assert len(reorders) >= 3
    for r in reorders:
        app.approvals.decide(r.id, "approve", OWNER, "test")
    calls = []
    report = app.procurement.run(batch_size=2, checkpoint=lambda stage, done, total: calls.append((stage, done, total)))
    n = report.considered
    assert n >= 3
    assert calls == [("proposing", d, n) for d in [*range(2, n, 2), n]]
    after = app.procurement.run(batch_size=2, checkpoint=lambda *_: None)
    assert after.considered == 0, "every approved reorder was taken once"


def test_an_item_whose_stock_is_fine_is_kept_as_a_few_fields_until_it_is_reconciled(make_app, clock):
    """On a site of tens of thousands of items nearly every item is fine, and a whole analysis is tens of kilobytes: a run of 30,000 items held more than a gigabyte."""
    from ai_munimji_engine.packs.stock_procurement.inventory.analysis import STATUS_OK

    app = make_app(erp=MockERP(today=clock().date()))
    seen, real = [], app.inventory._reconcile

    def watching(a, *args, **kwargs):
        seen.append(a)
        return real(a, *args, **kwargs)

    app.inventory._reconcile = watching
    app.inventory.run()
    fine = [a for a in seen if a.status == STATUS_OK]
    assert fine and len(fine) < len(seen), "the demo data has both kinds"
    assert all(not hasattr(a, "policy") and not hasattr(a, "usage") for a in fine)
    assert all(hasattr(a, "policy") and hasattr(a, "usage") for a in seen if a.status != STATUS_OK), "an item that needs a request keeps its whole analysis"


def test_the_window_an_item_is_read_for_does_not_depend_on_how_the_run_is_cut(make_app, clock):
    """Items that share their warehouses are read for the longest history any of them asks for. Cut into batches, an item whose batch held only items with a shorter history would be read
    for less than it was in one piece, and its analysis could differ."""
    class Spy(MockERP):
        starts: list = []

        def get_consumption(self, item_codes, start, end, warehouses=None):
            self.starts.append(start)
            return super().get_consumption(item_codes, start, end, warehouses)

    def windows(batch_size):
        erp = Spy(today=clock().date())
        erp.starts = []
        app = make_app(erp=erp)
        for code in ("BOLT-M8", "OIL-5L", "RELAY-12V"):
            app.settings.update_policy(OWNER, "item", code, {"history_days": 120})
        app.inventory.run(batch_size=batch_size, checkpoint=lambda *_: None)
        return set(erp.starts)

    whole, cut = windows(None), windows(1)
    assert len(whole) == 1, "one window for everyone in one piece"
    assert cut == whole, "and the same window when every item is its own batch"


def test_a_stage_that_writes_is_cut_by_the_clock_as_well_as_by_the_count():
    """What an item writes stays locked until the checkpoint commits, and a person waiting for that row waits that long: the stages that write are cut every so many seconds too."""
    from ai_munimji_engine.core.common import Batches

    now, calls = [0.0], []

    def cut(seconds, size=100):
        calls.clear()
        now[0] = 0.0
        batches = Batches("raising", 10, size, lambda *a: calls.append(a), seconds, clock=lambda: now[0])
        for _ in range(10):
            now[0] += 0.4
            batches.one()
        return [c[1] for c in calls]

    assert cut(1.0) == [3, 6, 9, 10], "a second has passed after the third item, and again after the sixth and the ninth; and the last item ends the stage"
    assert cut(None) == [10], "without a time limit only the count (and the end) cut the stage"
    assert cut(1.0, size=4) == [3, 4, 7, 8, 10], "whichever comes first: the clock at 3 (then the count at 4), the clock at 7 (then the count at 8), and the end"
    assert cut(1.0, size=None) == [], "no size, no checkpoints"


def test_the_raising_stage_is_cut_by_the_clock_and_the_reading_stage_is_not(make_app, clock):
    app = make_app(erp=MockERP(today=clock().date()))
    calls = []
    report = app.inventory.run(batch_size=1000, checkpoint=lambda stage, done, total: calls.append((stage, done)), batch_seconds=0)
    assert [c for c in calls if c[0] == "checking"] == [("checking", report.checked)], "reading: by the count and the end only"
    assert [d for s, d in calls if s == "raising"] == list(range(1, report.checked + 1)), "raising: after every item when the clock says so every time"


def test_a_run_takes_a_requests_row_before_its_alerts_as_a_persons_answer_does(make_app, clock):
    """A person's answer locks the request and then the alert (decide, then the handler). A run that locked the alert and then the request could deadlock with that person. The order is
    pinned where the run replaces or closes an alert and where it replaces or clears a stock alert."""
    erp = MockERP(today=clock().date())
    app = make_app(erp=erp, cfg={"system": {"stock_alerts": True}})
    app.inventory.run()
    trace = []
    real_cancel, real_alert, real_notice = app.approvals.cancel, app.alerts.update_status, app.inventory.notices.set_status
    app.approvals.cancel = lambda *a, **k: (trace.append("request"), real_cancel(*a, **k))[1]
    app.alerts.update_status = lambda *a, **k: (trace.append("alert"), real_alert(*a, **k))[1]
    app.inventory.notices.set_status = lambda *a, **k: (trace.append("notice"), real_notice(*a, **k))[1]
    for code in list(erp.items):
        erp.set_stock(code, on_hand=1_000_000)                          # everything is fine again: alerts are resolved and stock alerts cleared
    app.inventory.run()
    assert "alert" in trace and "notice" in trace, "the demo data had an alert and a stock alert to close"
    groups, current = [], []                                              # a group is one closing: the request's cancellation, and then what is written to the alert or the stock alert (twice for an alert:
    for kind in trace:                                                    # the cancellation sets it to "cancelled" itself, and the close sets its final status)
        if kind == "request":
            current = [kind]
            groups.append(current)
        else:
            assert current, f"{kind} was changed before any request was cancelled: {trace}"
            current.append(kind)
    assert groups and all(g[0] == "request" and len(g) >= 2 for g in groups), trace
