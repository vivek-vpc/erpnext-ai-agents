"""Posts the world of the shared ERP-client checks into an ERPNext site over REST:  python -m tools.testdata.cli contract --url ... --key ... --secret ...

The loading logic is in the engine (`ai_munimji_engine.testkit.loader`), once, so that the Frappe app can load its own bench with the same code and no HTTP; this module gives it
this tool's REST `Writer`."""
from __future__ import annotations

from ai_munimji_engine.testkit.loader import MARK, Loader, LoaderError, run

__all__ = ["MARK", "Loader", "LoaderError", "run"]
