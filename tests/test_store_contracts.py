"""The stores of every backend do the same thing.

The 60 checks are in the engine (`ai_munimji_engine.testkit.stores`, written once against the Protocols, no test framework in their imports); this file runs each of them once on SQLite
and once on the in-memory fake (the `stores` fixture of conftest.py, so 120 runs). The Frappe app (AI-Munimji) runs the same 60 on DocTypes on a bench. A new store comes with its Protocol,
its classes and a check there, and every backend has to pass it.
"""

import pytest

from ai_munimji_engine.testkit import stores as store_checks
from erp_agents.packs.stock_procurement.stores import stock_stores_for

NAMES = [c.name for c in store_checks.CHECKS]
BY_NAME = {c.name: c for c in store_checks.CHECKS}


def test_there_are_sixty_checks_each_with_its_own_name():
    # the number is pinned so that a check cannot disappear from the engine without this file saying so (it was 38 + 22 tests when they lived in two files here)
    assert len(NAMES) == 60 and len(set(NAMES)) == 60


@pytest.mark.parametrize("name", NAMES)
def test_the_stores(stores, name):
    BY_NAME[name].run(stores, stock_stores_for(stores))
