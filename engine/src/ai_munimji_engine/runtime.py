"""What the rule logic needs from the process it runs in: a way to run work in the background, a lock that lets one thing run at a time, and a memory for what a person was
just asked. Three small interfaces, each with the implementation the standalone service uses (threads and dictionaries in the process) and one for tests.

The standalone service starts a thread; a Frappe site queues a job (`frappe.enqueue`) and keeps the memory in its cache, because a site runs several workers that share nothing.
The rule logic is written against these interfaces and starts no thread of its own."""
from __future__ import annotations

import contextvars
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Callable, Optional, Protocol


# ---------------------------------------------------------------------------------------------------------------------- background work
class JobRunner(Protocol):
    def submit(self, fn: Callable[..., Any], *args: Any, name: str = "") -> None:
        """Run `fn(*args)` later or elsewhere, without making the caller wait for it. Whatever the caller had bound for logging (a request id) goes with the work where the
        runner can carry it. The caller never gets a result: work that has one writes it down first (an agent task has its row before it starts) and is polled for."""


class ThreadJobRunner:
    """One daemon thread per job, started at once, with a copy of the caller's context variables (a new thread starts with an empty context)."""

    def submit(self, fn: Callable[..., Any], *args: Any, name: str = "") -> None:
        context = contextvars.copy_context()
        threading.Thread(target=context.run, args=(fn, *args), name=name or None, daemon=True).start()


class PoolJobRunner:
    """A small fixed pool of threads: jobs wait their turn when all are busy (the chat on a phone is handled by two workers, so a slow answer does not hold up a button press)."""

    def __init__(self, workers: int = 2, prefix: str = "job") -> None:
        self._pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix=prefix)

    def submit(self, fn: Callable[..., Any], *args: Any, name: str = "") -> None:
        self._pool.submit(fn, *args)

    def shutdown(self, wait: bool = True) -> None:
        """Let the jobs already submitted finish (when `wait`) and take no more."""
        self._pool.shutdown(wait=wait)


class InlineJobRunner:
    """Runs the job in the caller's thread, before `submit` returns. For tests, and for a caller that is itself a job. An error in the job reaches the caller."""

    def submit(self, fn: Callable[..., Any], *args: Any, name: str = "") -> None:
        fn(*args)


# ---------------------------------------------------------------------------------------------------------------------- one at a time
class Exclusive(Protocol):
    """A lock that only one holder has at a time. In a process it is a thread lock; on a site with several workers it has to be kept in the database or the cache."""

    def acquire(self, timeout: float = -1) -> bool:
        """True when the caller has it now; False when `timeout` seconds passed without getting it (negative = wait as long as it takes)."""

    def release(self) -> None: ...

    def locked(self) -> bool: ...


class ThreadExclusive:
    def __init__(self) -> None:
        self._lock = threading.Lock()

    def acquire(self, timeout: float = -1) -> bool:
        return self._lock.acquire(timeout=timeout)

    def release(self) -> None:
        self._lock.release()

    def locked(self) -> bool:
        return self._lock.locked()


# ---------------------------------------------------------------------------------------------------------------------- what a person was just asked
@dataclass
class Awaiting:
    """The bot has asked this person something about a request (a quantity, a vendor): their next message is probably the answer, until `until`."""
    approval_id: str
    action: str
    choices: list[dict[str, str]]
    until: float


@dataclass
class ItemContext:
    """The items the last answer to this person was about, so "compare vendors" after a question about an item still means that item, until `until`."""
    items: list[str] = field(default_factory=list)
    until: float = 0.0


class ConversationMemory(Protocol):
    """Short-lived memory per chat, keyed by (channel, sender). Whoever reads a value checks its `until` against the clock: the memory never expires anything itself."""

    def expect(self, channel: str, sender: str, awaiting: Awaiting) -> None: ...

    def take_awaiting(self, channel: str, sender: str) -> Optional[Awaiting]:
        """The pending question for this chat, which is forgotten as it is taken (a message answers it or drops it); None when there is none."""

    def remember_items(self, channel: str, sender: str, context: ItemContext) -> None: ...

    def recall_items(self, channel: str, sender: str) -> Optional[ItemContext]: ...


class InProcessConversationMemory:
    def __init__(self) -> None:
        self._awaiting: dict[tuple[str, str], Awaiting] = {}
        self._items: dict[tuple[str, str], ItemContext] = {}
        self._lock = threading.Lock()

    def expect(self, channel: str, sender: str, awaiting: Awaiting) -> None:
        with self._lock:
            self._awaiting[(channel, sender)] = awaiting

    def take_awaiting(self, channel: str, sender: str) -> Optional[Awaiting]:
        with self._lock:
            return self._awaiting.pop((channel, sender), None)

    def remember_items(self, channel: str, sender: str, context: ItemContext) -> None:
        with self._lock:
            self._items[(channel, sender)] = context

    def recall_items(self, channel: str, sender: str) -> Optional[ItemContext]:
        with self._lock:
            return self._items.get((channel, sender))


# ---------------------------------------------------------------------------------------------------------------------- the bundle the application is built with
@dataclass
class Runtime:
    jobs: JobRunner
    exclusive: Callable[[], Exclusive]          # makes a lock: the agents take one, the conversation none
    memory: ConversationMemory


def thread_runtime() -> Runtime:
    """What the standalone service uses: threads, thread locks and dictionaries in the process."""
    return Runtime(jobs=ThreadJobRunner(), exclusive=ThreadExclusive, memory=InProcessConversationMemory())


def inline_runtime() -> Runtime:
    """For tests that want every background job to finish before the call returns."""
    return Runtime(jobs=InlineJobRunner(), exclusive=ThreadExclusive, memory=InProcessConversationMemory())
