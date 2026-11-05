"""The storage interface of the stock pack: one Protocol per group of its tables, a `Memory*Store` beside each, and the bundle the pack is set up with.

The pack's domain code (alerts, proposals, the follow-up, counts, transfers) is given a `StockStores`; the standalone service builds the SQLite one
(`erp_agents.packs.stock_procurement.stores`), a test can build the memory one, and the Frappe app will build one over DocTypes."""
from __future__ import annotations

from dataclasses import dataclass

from ...storage import MemoryDb
from .alerts import AlertStore, MemoryAlertStore
from .assistant import AssistantStore, MemoryAssistantStore
from .followup import ChaseStore, MemoryChaseStore, MemoryQuoteStore, MemoryReceiptStore, QuoteStore, ReceiptStore
from .movements import CountStore, MemoryCountStore, MemoryTransferStore, TransferStore
from .notices import MemoryNoticeStore, NoticeStore
from .proposals import MemoryProposalStore, ProposalStore
from .runs import AgentRunStore, MemoryAgentRunStore
from .vendors import MemoryVendorStore, VendorStore


@dataclass
class StockStores:
    alerts: AlertStore
    notices: NoticeStore
    proposals: ProposalStore
    runs: AgentRunStore
    assistant: AssistantStore
    vendors: VendorStore
    chases: ChaseStore
    receipts: ReceiptStore
    quotes: QuoteStore
    transfers: TransferStore
    counts: CountStore


def memory_stock_stores(db: MemoryDb) -> StockStores:
    """The stock pack's stores in memory, over the same `MemoryDb` as the core's (so one transaction covers both)."""
    return StockStores(alerts=MemoryAlertStore(db), notices=MemoryNoticeStore(db), proposals=MemoryProposalStore(db), runs=MemoryAgentRunStore(db),
                       assistant=MemoryAssistantStore(db), vendors=MemoryVendorStore(db), chases=MemoryChaseStore(db), receipts=MemoryReceiptStore(db),
                       quotes=MemoryQuoteStore(db), transfers=MemoryTransferStore(db), counts=MemoryCountStore(db))
