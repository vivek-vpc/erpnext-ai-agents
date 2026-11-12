# Changelog

What changed for someone who runs the service or builds on the engine. `ai-munimji-engine` 0.1.0 (9 Nov 2026) is on PyPI and cannot be changed; 0.1.1 carries the fixes after review.

## Unreleased

### Added for a large site (engine 0.1.2 when it is released)

From the production-readiness review (findings A1 and A2). Nothing changes for a caller that does not use the new parameters: the standalone service runs as before.

* **A long stock check can work in batches.** `InventoryAgent.run(dry_run, item_codes, batch_size, checkpoint)` and `ProcurementAgent.run(dry_run, batch_size, checkpoint)` take an optional `batch_size` and a
  `checkpoint(stage, done, total)` that they call at the end of every batch ("checking": reading and analysing, which writes nothing; "raising": alerts and requests, worst first; "proposing": purchase proposals). The
  caller commits there, renews its lease and notes how far the run has got; an exception raised from the checkpoint stops the run, and what the batches before it did stays done. The result is the same as without
  batches: the same items are analysed with the same figures (the longest history any item of a set of warehouses asks for is the window for all of them) and acted on in the same order, so a run that stops half way
  and is run again acts on each item once (the duplicate checks of `_reconcile` and `_notify` find what the first run raised). `tests/test_batched_runs.py`. Helpers: `Batches` and `slices` in `core/common.py`.
* **The stages that write are also cut by the clock.** `run(..., batch_seconds=)` ends a batch of the "raising" and "proposing" stages that long after the last checkpoint, however few items it holds: what an item
  writes stays locked until the caller commits, and a person waiting for that row waits that long (`core/common.py`: `Batches(seconds=)`).
* **A run takes a request's row before its alert's, as a person's answer does.** Replacing or closing an alert, and replacing or clearing a stock alert, wrote the alert's (or stock alert's) status and then
  cancelled its request; a person's answer locks the request and then the alert. Run at the same moment, each could hold one row and wait for the other, and the database ended one of them with a deadlock. The request is
  now cancelled first (the end state is the same). `tests/test_batched_runs.py`.
* **A look at one item no longer lists every item.** An ERP client may offer an optional `get_items(codes)`; the inventory agent uses it for a run over named items and for the analysis of one item (which is what
  answering a reorder request does), and lists all items otherwise, as before. `tests/test_one_item_lookup.py`.
* **An item whose stock is fine is kept as four fields until the end of the run.** Its whole analysis (the usage series, the reasons, the rules, the policy: tens of kilobytes) is not needed once its status is known to
  be fine; a run over 30,000 items held more than a gigabyte of them.
* **The vendors can be compared for one reorder, at once.** `ProcurementAgent.run(..., work_orders=[ids])` takes only those work orders, and only those still waiting (one that another run has claimed is left alone, so a reorder is compared
  once however many runs look at it); the others stay as they are and the snoozed requests whose time has come are not touched, which is the scheduled run's. The Frappe app uses it to compare the vendors within
  a minute of a person approving a reorder, instead of at the next scheduled run (Group 2, F1). Without the parameter the run is what it was. `tests/test_one_work_order.py`.

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
