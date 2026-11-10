# Changelog

What changed for someone who runs the service or builds on the engine. `ai-munimji-engine` 0.1.0 (9 Nov 2026) is on PyPI and cannot be changed; 0.1.1 carries the fixes after review.

## 0.1.1 (10 Nov 2026)

### Fixed after review

All in the stock and purchasing pack; each has a test that failed before the fix (`src/erp_agents/packs/stock_procurement/tests/test_review_fixes.py`). Nothing else changed: the dependencies (pydantic and PyYAML), the stores and their schema, and the interfaces are the same as in 0.1.0, so 0.1.1 replaces it as it is. The Frappe app 0.1.1 requires this version.

* **An item with a very large stock is no longer dropped from monitoring.** 100,000 units on hand with one sale of 1 unit in 90 days is millions of days of cover, and `today + timedelta(...)` raised
  `OverflowError`; the run logged it and the item was never looked at again. Now no date is worked out more than 10 years away: the stock-out date (and the latest order date) is left empty, the cover
  stays a number, and the analysis says "there is more than 10 years of cover at the current usage". The same limit protects the other dates built from a cover or a lead time (the replenishment date, the
  forecast's cut-off date, the expected arrival of an order), and the day-by-day sums stop at ten years and carry on at the usual rate.
* **A lead time that is not a number, or is more than 10 years, is ignored.** On the item record or in the settings it is a typing mistake; the analysis uses the default lead time and says so.
* **A price that is not a positive number can no longer win.** A negative price used to beat every real one (`if not price` let it through), and a NaN passed as well. A vendor whose price on record (last
  order, average, or supplier quotation) is zero, negative or not a number is out of the comparison, with the reason "the price on record is not a positive number: check the last order in the ERP".
* **The on-time rate is kept between 0 and 1** before it is scored (1.7 had scored 170 of 100 points); one that is not a number counts as unknown.
* **A typed quantity with a decimal comma is read as one.** "12,5" is 12.5, not 125. One comma, no dot and one or two digits after it is a decimal comma; every other comma is a thousands separator
  ("1,000", "1,00,000"), and an ambiguous "1,500" stays 1500. A comma after the decimal point ("1.234,56") is refused rather than guessed. The same reader is used for a number a tool is given as text.
* **A total before tax next to totals with tax is said to be incomparable.** A vendor with no tax setup looked cheaper than one with taxes and could win without a word. The ranking is unchanged, but
  the request now says, right under the total, which vendors have no tax setup and that their totals are before tax and not comparable; the same sentence is on the ranking and on that vendor's flags.
* **A purchase return is not a delivery.** In the vendor scorecard a receipt line with a negative quantity counted as a delivery (and a late one, if it was posted late). It is left out.

## 0.1.0 (9 Nov 2026)

### Publishing the engine

* **A workflow that publishes `ai-munimji-engine` to PyPI** when a tag `engine-vX.Y.Z` is pushed (`.github/workflows/publish-engine.yml`), by PyPI's trusted publishing (no token stored), after the
  protected environment `pypi` is approved, with every action pinned by commit SHA. On every pull request the same file runs as a dry run: build, `twine check --strict`, and a check that the names PyPI
  was registered with, the permissions and the pins are still in it (`tools/check_publish_workflow.py`, `tests/test_publish_workflow.py`). The steps that are the owner's are in
  [docs/PUBLISH_ENGINE.md](docs/PUBLISH_ENGINE.md). The tag `engine-v0.1.0` was pushed on 9 Nov 2026 and `ai-munimji-engine` 0.1.0 is on PyPI.

### Changed behaviour of the service

* **A purchase request keeps the tax figures it was made with.** When someone changes the quantity or the vendor of a request, or the request is rebuilt, the totals are worked out from the
  tax figures the approver was shown, not from a fresh read of the ERP's tax setup; the ERP is asked again only for a vendor whose figures could not be read the first time. A request made
  when no tax figures could be read at all now says "Taxes are not included in these totals" instead of showing a total without saying so (the older wording stays for an ERP that applies no
  tax). Why: in the Frappe app the person who presses the button may not be allowed to read Tax Rules; the service gets the same rule so that the two behave alike.
  * **No migration, and none is needed.** The figures are one more key (`tax_setups`) inside `proposals.ranking_json`, a text column that has always been there. The schema is exactly the one
    the code made before the work on the Frappe app began: `tests/test_schema_unchanged.py` pins it to commit 78372cb and passes.
  * **A database made before this opens as it was**, with every row unchanged and no copy of the file made (`test_tax_figures_old_data.py`). **A request made before this carries no figures**
    and behaves as it did: changing its quantity asks the ERP for each vendor's tax setup, and from then on the request carries them.

### Added

* The stores' shared checks (60) are in the engine (`ai_munimji_engine.testkit.stores`), written once against the Protocols; SQLite and memory run them here (120 runs), the Frappe app runs
  them on DocTypes. One known history and its audit seal (`AUDIT_GOLDEN`) are pinned the same way.
* The ERP port's shared checks (44, `ai_munimji_engine.testkit`) with a small neutral world, run on the demo ERP, the REST client and (in the Frappe app) the Frappe client.
* The stock pack's `manifest.yaml` is read from the engine wheel.

### Fixed

* The shared check of an item's creation day no longer depends on the time zone (a site in Asia/Kolkata is a day ahead of UTC for five and a half hours of every day).
