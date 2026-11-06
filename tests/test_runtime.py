"""Background work, the one-at-a-time lock and the chat memory sit behind small interfaces (ai_munimji_engine.runtime), so the Frappe app can give them a queue and a cache.

These tests fix what each implementation promises, and (at the bottom) that the services use them instead of starting a thread of their own."""
import contextvars
import threading
import time

import pytest

from ai_munimji_engine.runtime import (Awaiting, InlineJobRunner, InProcessConversationMemory, ItemContext, PoolJobRunner, ThreadExclusive, ThreadJobRunner, inline_runtime,
                                       thread_runtime)

REQUEST_ID = contextvars.ContextVar("request_id", default="none")


def wait_for(predicate, seconds=5.0):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.01)
    return False


# ------------------------------------------------------------------ background work
def test_a_thread_runner_runs_the_job_in_another_thread_and_carries_the_callers_context():
    seen = {}
    done = threading.Event()

    def job(a, b):
        seen.update(thread=threading.current_thread(), request=REQUEST_ID.get(), args=(a, b))
        done.set()

    REQUEST_ID.set("req-42")
    ThreadJobRunner().submit(job, 1, 2, name="agent-test")
    assert done.wait(5)
    assert seen["thread"] is not threading.current_thread() and seen["thread"].daemon and seen["thread"].name == "agent-test"
    assert seen["request"] == "req-42" and seen["args"] == (1, 2)               # what the caller had bound for its log lines went with the work
    REQUEST_ID.set("none")


def test_the_caller_does_not_wait_for_a_thread_job():
    release = threading.Event()
    started = time.monotonic()
    ThreadJobRunner().submit(release.wait, 5)
    assert time.monotonic() - started < 1                                        # submit returned at once
    release.set()


def test_a_pool_runs_two_jobs_at_once_and_makes_a_third_wait():
    pool = PoolJobRunner(workers=2, prefix="chat")
    running, release, order = [], threading.Event(), []

    def job(label):
        running.append(label)
        release.wait(5)
        order.append(label)

    for label in "abc":
        pool.submit(job, label)
    assert wait_for(lambda: len(running) == 2) and not wait_for(lambda: len(running) == 3, 0.3)         # the third is waiting for a worker
    release.set()
    pool.shutdown(wait=True)                                                     # returns when all three have finished
    assert sorted(order) == ["a", "b", "c"]


def test_an_inline_runner_finishes_the_job_before_submit_returns_and_lets_its_error_through():
    ran = []
    InlineJobRunner().submit(ran.append, "x")
    assert ran == ["x"]
    with pytest.raises(ZeroDivisionError):
        InlineJobRunner().submit(lambda: 1 / 0)


# ------------------------------------------------------------------ one at a time
def test_the_exclusive_lock_is_held_by_one_and_times_out_for_the_next():
    lock = ThreadExclusive()
    assert not lock.locked() and lock.acquire(timeout=0.1) and lock.locked()
    assert lock.acquire(timeout=0.05) is False                                   # a second caller gives up after its timeout
    lock.release()
    assert not lock.locked() and lock.acquire() is True
    lock.release()


# ------------------------------------------------------------------ what a person was just asked
def test_a_pending_question_is_taken_once_and_forgotten():
    memory = InProcessConversationMemory()
    assert memory.take_awaiting("telegram", "chat-1") is None
    memory.expect("telegram", "chat-1", Awaiting("req-1", "change_qty", [], until=100.0))
    memory.expect("telegram", "chat-2", Awaiting("req-2", "change_vendor", [{"id": "S1"}], until=100.0))
    first = memory.take_awaiting("telegram", "chat-1")
    assert (first.approval_id, first.action, first.until) == ("req-1", "change_qty", 100.0)
    assert memory.take_awaiting("telegram", "chat-1") is None                   # taken: gone
    assert memory.take_awaiting("whatsapp", "chat-2") is None                   # the same sender on another channel is another chat
    assert memory.take_awaiting("telegram", "chat-2").choices == [{"id": "S1"}]
    memory.expect("telegram", "chat-1", Awaiting("req-1", "change_qty", [], 1.0))
    memory.expect("telegram", "chat-1", Awaiting("req-3", "change_qty", [], 2.0))
    assert memory.take_awaiting("telegram", "chat-1").approval_id == "req-3"    # a newer question replaces the older


def test_the_items_a_chat_was_about_are_remembered_until_they_are_replaced():
    memory = InProcessConversationMemory()
    assert memory.recall_items("telegram", "chat-1") is None
    memory.remember_items("telegram", "chat-1", ItemContext(["BOLT-M8"], until=50.0))
    assert memory.recall_items("telegram", "chat-1").items == ["BOLT-M8"] and memory.recall_items("telegram", "chat-1").until == 50.0
    assert memory.recall_items("telegram", "chat-1").items == ["BOLT-M8"]       # reading does not use it up
    memory.remember_items("telegram", "chat-1", ItemContext(["NUT-1"], until=60.0))
    assert memory.recall_items("telegram", "chat-1").items == ["NUT-1"]


def test_the_two_ready_made_runtimes():
    threads, inline = thread_runtime(), inline_runtime()
    assert isinstance(threads.jobs, ThreadJobRunner) and isinstance(inline.jobs, InlineJobRunner)
    assert isinstance(threads.exclusive(), ThreadExclusive) and threads.exclusive() is not threads.exclusive()       # each caller gets a lock of its own
    assert isinstance(threads.memory, InProcessConversationMemory) and inline.memory is not threads.memory


# ------------------------------------------------------------------ the services use them
def test_no_service_starts_a_thread_of_its_own():
    """Only the runtime (the standalone service's implementation of the interfaces) and the scheduler (an edge: the Telegram poller, which a site does not have) start threads."""
    import ast
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent
    allowed = {"engine/src/ai_munimji_engine/runtime.py", "src/erp_agents/core/scheduler.py"}
    offenders = []
    for base in ("src/erp_agents", "engine/src"):
        for path in (root / base).rglob("*.py"):
            rel = path.relative_to(root).as_posix()
            if "/tests/" in rel or rel in allowed:
                continue
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if isinstance(node, ast.Call):
                    name = getattr(node.func, "attr", getattr(node.func, "id", ""))
                    if name in ("Thread", "ThreadPoolExecutor", "Timer"):
                        offenders.append(f"{rel}:{node.lineno}")
    assert not offenders, "start background work through app.runtime.jobs, not a thread of your own: " + ", ".join(offenders)


def test_the_application_is_built_with_a_runtime_and_the_services_share_it(make_app):
    app = make_app()
    assert app.runtime is not None and app.agents.runtime is app.runtime and app.conversation.memory is app.runtime.memory
    from ai_munimji_engine.runtime import inline_runtime
    from erp_agents.core.bootstrap import build_app
    own = inline_runtime()
    other = build_app(config=app.config, clock=app.clock, erp=app.erp, channels={}, db_path=":memory:", runtime=own)
    assert other.runtime is own and other.agents.runtime is own
