# Changelog

What changed for someone who runs the service or builds on the engine. Nothing here is released to PyPI; the version numbers have not moved.

## Unreleased

### Publishing the engine (nothing is published yet)

* **A workflow that publishes `ai-munimji-engine` to PyPI** when a tag `engine-vX.Y.Z` is pushed (`.github/workflows/publish-engine.yml`), by PyPI's trusted publishing (no token stored), after the
  protected environment `pypi` is approved, with every action pinned by commit SHA. On every pull request the same file runs as a dry run: build, `twine check --strict`, and a check that the names PyPI
  was registered with, the permissions and the pins are still in it (`tools/check_publish_workflow.py`, `tests/test_publish_workflow.py`). The steps that are the owner's are in
  [docs/PUBLISH_ENGINE.md](docs/PUBLISH_ENGINE.md). No tag has been pushed and nothing is on PyPI.

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
