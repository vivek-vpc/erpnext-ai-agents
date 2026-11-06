"""Messages sent to vendors and the replies that came back (the tables `vendor_messages` and `vendor_replies`). `VendorMessenger`
(packs/stock_procurement/followup/vendors.py) is the domain code; this is the storage it needs. No contact data is stored: a message keeps only a keyed hash of the number or
address, to recognise a reply, and the hash is erased when the reply window ends.

A row of `vendor_messages`: id, purpose (chase | quote), ref_id, supplier, channel, address_hash, hash_kind, status (sent | failed), error, approval_id, sent_by, sent_at,
closed_at. A row of `vendor_replies`: id (1, 2, 3, ... in the order written), message_id, supplier, purpose, ref_id, channel, text, received_at, relayed (0 | 1 | 2), text_hash."""
from __future__ import annotations

from typing import ContextManager, Optional, Protocol

from ...storage import MemoryDb, Row


class VendorStore(Protocol):
    def transaction(self) -> ContextManager[None]: ...

    # ---- messages
    def add_message(self, message_id: str, purpose: str, ref_id: str, supplier: str, channel: str, address_hash: Optional[str], hash_kind: Optional[str], status: str,
                    error: Optional[str], approval_id: Optional[str], sent_by: Optional[str], sent_at: str) -> None: ...

    def messages(self, purpose: str, ref_id: str) -> list[Row]:
        """id, supplier, channel, status, error, approval_id, sent_at of the messages about one order or quote round, oldest sent first."""

    def messages_for_approval(self, approval_id: str) -> list[Row]:
        """supplier, channel, status, error, sent_at of the messages sent for one request, oldest sent first."""

    def close_replies(self, purpose: str, ref_id: str, at: str) -> int:
        """Mark the not yet closed messages about this order or round closed at `at` and erase their hash. Returns how many."""

    def forget_hashes_before(self, cutoff: str) -> int:
        """Erase the hash of every message sent before `cutoff` that still has one. Returns how many."""

    def open_message(self, address_hash: str, hash_kind: str, since: str) -> Optional[Row]:
        """The latest message (by sent_at) with this hash and kind ('' = no kind), status 'sent', sent at or after `since` and not closed; the whole row. None if none."""

    def open_chase_refs(self) -> set[str]:
        """The `ref_id` of every chase message that is not closed."""

    def last_sent_channel(self, approval_id: str) -> Optional[str]:
        """The channel of the newest message with status 'sent' for this request, or None."""

    # ---- replies
    def add_reply(self, message_id: str, supplier: str, purpose: str, ref_id: str, channel: str, text: str, received_at: str, relayed: int, text_hash: str) -> None: ...

    def replies(self, purpose: str, ref_id: str) -> list[Row]:
        """supplier, channel, text, received_at of the replies about one order or round, in the order they were written."""

    def replies_for_approval(self, approval_id: str) -> list[Row]:
        """supplier, text, received_at of the replies to the messages sent for one request, in the order they were written."""

    def reply_seen(self, supplier: str, text_hash: str, since: str) -> bool:
        """Is there a reply from the supplier with this text hash received at or after `since`?"""

    def count_replies(self, supplier: str, since: str, relayed: Optional[int] = None) -> int:
        """How many replies from the supplier were received at or after `since`; with `relayed`, only those with that value."""


class MemoryVendorStore:
    def __init__(self, db: MemoryDb) -> None:
        self.db = db

    def transaction(self) -> ContextManager[None]:
        return self.db.transaction()

    # ---- messages
    def add_message(self, message_id: str, purpose: str, ref_id: str, supplier: str, channel: str, address_hash: Optional[str], hash_kind: Optional[str], status: str,
                    error: Optional[str], approval_id: Optional[str], sent_by: Optional[str], sent_at: str) -> None:
        with self.db.lock:
            if any(r["id"] == message_id for r in self.db.rows("vendor_messages")):
                raise ValueError(f"message {message_id} exists")
            self.db.insert("vendor_messages", {"id": message_id, "purpose": purpose, "ref_id": ref_id, "supplier": supplier, "channel": channel, "address_hash": address_hash,
                                               "status": status, "error": error, "approval_id": approval_id, "sent_by": sent_by, "sent_at": sent_at, "hash_kind": hash_kind,
                                               "closed_at": None})

    def messages(self, purpose: str, ref_id: str) -> list[Row]:
        with self.db.lock:
            rows = sorted((r for r in self.db.rows("vendor_messages") if r["purpose"] == purpose and r["ref_id"] == ref_id), key=lambda r: r["sent_at"])
            return [{k: r[k] for k in ("id", "supplier", "channel", "status", "error", "approval_id", "sent_at")} for r in rows]

    def messages_for_approval(self, approval_id: str) -> list[Row]:
        with self.db.lock:
            rows = sorted((r for r in self.db.rows("vendor_messages") if r["approval_id"] == approval_id), key=lambda r: r["sent_at"])
            return [{k: r[k] for k in ("supplier", "channel", "status", "error", "sent_at")} for r in rows]

    def close_replies(self, purpose: str, ref_id: str, at: str) -> int:
        with self.db.lock:
            rows = [r for r in self.db.rows("vendor_messages") if r["purpose"] == purpose and r["ref_id"] == ref_id and r["closed_at"] is None]
            for r in rows:
                r["closed_at"], r["address_hash"] = at, None
            return len(rows)

    def forget_hashes_before(self, cutoff: str) -> int:
        with self.db.lock:
            rows = [r for r in self.db.rows("vendor_messages") if r["address_hash"] is not None and r["sent_at"] < cutoff]
            for r in rows:
                r["address_hash"] = None
            return len(rows)

    def open_message(self, address_hash: str, hash_kind: str, since: str) -> Optional[Row]:
        with self.db.lock:
            rows = [r for r in self.db.rows("vendor_messages") if r["address_hash"] == address_hash and (r["hash_kind"] or "") == hash_kind and r["status"] == "sent"
                    and r["sent_at"] >= since and r["closed_at"] is None]
            ordered = sorted(rows, key=lambda r: r["sent_at"], reverse=True)
            return dict(ordered[0]) if ordered else None

    def open_chase_refs(self) -> set[str]:
        with self.db.lock:
            return {r["ref_id"] for r in self.db.rows("vendor_messages") if r["purpose"] == "chase" and r["closed_at"] is None}

    def last_sent_channel(self, approval_id: str) -> Optional[str]:
        with self.db.lock:
            rows = sorted((r for r in self.db.rows("vendor_messages") if r["approval_id"] == approval_id and r["status"] == "sent"), key=lambda r: r["sent_at"], reverse=True)
            return rows[0]["channel"] if rows else None

    # ---- replies
    def add_reply(self, message_id: str, supplier: str, purpose: str, ref_id: str, channel: str, text: str, received_at: str, relayed: int, text_hash: str) -> None:
        self.db.insert("vendor_replies", {"message_id": message_id, "supplier": supplier, "purpose": purpose, "ref_id": ref_id, "channel": channel, "text": text,
                                          "received_at": received_at, "relayed": relayed, "text_hash": text_hash}, auto_id="id")

    def replies(self, purpose: str, ref_id: str) -> list[Row]:
        with self.db.lock:
            rows = sorted((r for r in self.db.rows("vendor_replies") if r["purpose"] == purpose and r["ref_id"] == ref_id), key=lambda r: r["id"])
            return [{k: r[k] for k in ("supplier", "channel", "text", "received_at")} for r in rows]

    def replies_for_approval(self, approval_id: str) -> list[Row]:
        with self.db.lock:
            messages = {m["id"] for m in self.db.rows("vendor_messages") if m["approval_id"] == approval_id}
            rows = sorted((r for r in self.db.rows("vendor_replies") if r["message_id"] in messages), key=lambda r: r["id"])
            return [{k: r[k] for k in ("supplier", "text", "received_at")} for r in rows]

    def reply_seen(self, supplier: str, text_hash: str, since: str) -> bool:
        with self.db.lock:
            return any(r["supplier"] == supplier and r["text_hash"] == text_hash and r["received_at"] >= since for r in self.db.rows("vendor_replies"))

    def count_replies(self, supplier: str, since: str, relayed: Optional[int] = None) -> int:
        with self.db.lock:
            return sum(1 for r in self.db.rows("vendor_replies") if r["supplier"] == supplier and r["received_at"] >= since and (relayed is None or r["relayed"] == relayed))
