# ai-munimji-engine

The rule logic behind AI Munimji, as a Python package (`ai_munimji_engine`) with no web server, no database driver and no network call in it. It decides; a person approves.

**Status.** Built from the repository [erpnext-ai-agents](https://github.com/vivek-vpc/erpnext-ai-agents), where it lives in `engine/`. It is not published on PyPI. The AI Munimji app for Frappe and
ERPNext does not exist as a release yet, and nothing here is on the Frappe Marketplace.

## What is in it

* `core/`: the approval workflow (a request, its answer, its expiry; exactly one answer ever wins), the audit seal over the decision history, the settings store, the tool registry with its role
  checks, logging and metrics helpers. Nothing in it knows about stock or vendors.
* `packs/stock_procurement/`: stock analysis and forecasts, alerts, vendor scoring with taxes and landed cost, purchase proposals and the requests that carry them, the seven stock tools.
* `stores/`: the storage the rule logic is written against, as `Protocol`s with an in-memory implementation beside each (`MemoryDb`). The standalone service gives them SQLite; the Frappe app gives
  them DocTypes. `runtime.py`: background work, the one-at-a-time lock and the chat memory, as three small interfaces.
* `core/erp/base.py` and `packs/stock_procurement/erp.py`: the ports to the ERP (what is read, what is created as a draft).

## What it needs

`pydantic` and `PyYAML`, and the standard library. On a system without a time zone database (Windows) add the extra: `pip install "ai-munimji-engine[tzdata]"`; the optional tracing is the extra `otel`. Python 3.10 or later.
A test refuses any other import, and refuses `fastapi`, `starlette`, `uvicorn`, `apscheduler`, `sqlite3`, `mcp`, `requests` and the other network libraries at import time.

## Using it

```python
from ai_munimji_engine.stores import memory_core_stores          # the whole core on dictionaries: no database
stores = memory_core_stores()
```

The application that wires everything together is not part of the engine: the standalone service does it in `erp_agents.core.bootstrap`, the Frappe app will do it with its own stores and ERP port.

## Licence

GPL-3.0-or-later, like the rest of the repository.
