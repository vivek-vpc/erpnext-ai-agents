"""The ERP port every pack shares. A pack's own reads and writes are in its own port (the stock pack's: `ai_munimji_engine.packs.stock_procurement.erp`), so this stays
small: is the ERP reachable, and can a document this application made be found again by the reference it was made with."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Protocol, runtime_checkable


@runtime_checkable
class ERPClient(Protocol):
    def healthcheck(self) -> bool: ...


@dataclass(frozen=True)
class FoundDocument:
    """A document this application made earlier, found again by the reference it was made with (see `RecoveryERP`)."""
    doctype: str
    name: str
    approved_by: str = ""                    # what the application wrote on it when it made it: "<person> via <channel>"


@runtime_checkable
class RecoveryERP(Protocol):
    """Finding a document by its reference. Optional, like `FollowUpERP`: a connection without it cannot finish an answer that was acted on but never written down (call it
    through `getattr`)."""

    def find_by_reference(self, doctype: str, reference: str) -> Optional[FoundDocument]:
        """The not-cancelled document of this type made for `reference`, or None. Raises when the ERP cannot be asked."""
        ...
