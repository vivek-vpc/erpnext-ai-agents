"""Looking at one item does not read every item (finding A2 of the production-readiness review).

On a site with tens of thousands of items, listing them all costs seconds, and the listing used to be made for every answer to a reorder request (the request is read again with today's
figures). An ERP that can read some items without listing the rest says so with an optional `get_items(codes)`; the inventory agent uses it for a run over named items and for the
analysis of one item. An ERP without it is listed in full as before."""
import pytest

from erp_agents.core.erp.mock import MockERP


class ItemsOnRequest(MockERP):
    """A demo ERP that can read items one by one, and fails if anybody lists them all."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.listed_all, self.asked_for = 0, []

    def list_items(self):
        self.listed_all += 1
        return super().list_items()

    def get_items(self, item_codes):
        self.asked_for.append(list(item_codes))
        return [m.info for code, m in self.items.items() if code in item_codes]


def test_the_analysis_of_one_item_reads_that_item_and_does_not_list_them_all(make_app, clock):
    erp = ItemsOnRequest(today=clock().date())
    app = make_app(erp=erp)
    analysis, _ = app.inventory.analyze_one("BOLT-M8")
    assert analysis.item_code == "BOLT-M8"
    assert (erp.listed_all, erp.asked_for) == (0, [["BOLT-M8"]])


def test_an_unknown_item_is_still_unknown(make_app, clock):
    erp = ItemsOnRequest(today=clock().date())
    app = make_app(erp=erp)
    with pytest.raises(KeyError, match="NO-SUCH-ITEM"):
        app.inventory.analyze_one("NO-SUCH-ITEM")


def test_a_run_over_named_items_reads_only_those_and_a_run_over_all_lists_them(make_app, clock):
    erp = ItemsOnRequest(today=clock().date())
    app = make_app(erp=erp)
    report = app.inventory.run(item_codes=["BOLT-M8", "OIL-5L"])
    assert report.checked == 2
    assert (erp.listed_all, erp.asked_for) == (0, [["BOLT-M8", "OIL-5L"]])
    app.inventory.run()
    assert erp.listed_all == 1, "without names the whole list is what the run is about"


def test_the_result_is_the_one_an_erp_without_the_method_gives(make_app, clock):
    with_it = make_app(erp=ItemsOnRequest(today=clock().date()))
    without = make_app(erp=MockERP(today=clock().date()))
    a, b = with_it.inventory.run(item_codes=["BOLT-M8", "OIL-5L"]), without.inventory.run(item_codes=["BOLT-M8", "OIL-5L"])
    assert sorted((x["item_code"], x["action"]) for x in a.actions) == sorted((x["item_code"], x["action"]) for x in b.actions)
    one, _ = with_it.inventory.analyze_one("OIL-5L")
    other, _ = without.inventory.analyze_one("OIL-5L")
    assert (one.status, one.severity, one.on_hand) == (other.status, other.severity, other.on_hand)
