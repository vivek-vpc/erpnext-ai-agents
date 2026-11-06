"""Tests of the ERP port, written once and run against every ERP client.

`world` is a small dataset in neutral words, with what each read of it must return worked out from its own documents. `contract` holds the checks: plain functions that raise
`AssertionError`, run by `run_checks(client, site)`. No test framework is imported here (the engine's runtime imports stay pydantic and PyYAML only); pytest in the standalone
service, and unittest in the Frappe app, call the same functions.

    from ai_munimji_engine.testkit import contract
    results = contract.run_checks(client, contract.Site(today=..., names={...}))
"""
