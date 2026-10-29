"""The circuit breaker on a `requests` session (core/httpguard.py): what counts as the service being down, and what does not (Phase 3 of the hardening brief)."""
import pytest
import requests
from requests.adapters import BaseAdapter

from erp_agents.core import httpguard, resilience
from erp_agents.core.httpguard import CircuitOpen, guard_session


class Net(BaseAdapter):
    """A network that answers from a script: a status, or an exception to raise. Counts how many requests really went out."""

    def __init__(self, *script):
        super().__init__()
        self.script, self.sent = list(script), 0

    def send(self, request, **kwargs):
        self.sent += 1
        step = self.script.pop(0) if len(self.script) > 1 else self.script[0]
        if isinstance(step, BaseException):
            raise step
        r = requests.Response()
        r.status_code, r._content, r.url, r.request = step, b"{}", request.url, request
        return r

    def close(self):
        pass


class Time:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


@pytest.fixture
def clock():
    return Time()


def guarded(net, clock, failures=3, pause=30, name="erpnext"):
    resilience.configure(resilience.ResilienceSettings(erpnext_failures=failures, erpnext_pause_seconds=pause), clock=clock)
    s = requests.Session()
    s.mount("http://", net)
    return guard_session(s, name)


def get(s):
    return s.get("http://erp.example.test/api/resource/Item", timeout=5).status_code


def test_a_server_that_does_not_answer_is_asked_only_as_often_as_the_limit_then_not_at_all(clock):
    net = Net(requests.ConnectionError("refused"))
    s = guarded(net, clock, failures=3)
    for _ in range(3):
        with pytest.raises(requests.ConnectionError):
            get(s)
    for _ in range(10):
        with pytest.raises(CircuitOpen):
            get(s)
    assert net.sent == 3                                                              # the ten later calls never left this computer


def test_the_refusal_is_a_connection_error_so_everything_that_already_handles_one_still_works(clock):
    s = guarded(Net(requests.ConnectionError("x")), clock, failures=1)
    with pytest.raises(requests.ConnectionError):
        get(s)
    with pytest.raises(requests.ConnectionError) as refused:                          # (what `except requests.RequestException` in the ERP client catches)
        get(s)
    assert isinstance(refused.value, CircuitOpen) and isinstance(refused.value, requests.RequestException)
    assert "ERPNext is not answering" in str(refused.value) and refused.value.retry_in > 0


@pytest.mark.parametrize("failure", [requests.ReadTimeout("slow"), requests.ConnectTimeout("slow"), requests.exceptions.SSLError("tls"), 500, 502, 503, 504, 529])
def test_what_counts_as_the_service_being_down(clock, failure):
    s = guarded(Net(failure), clock, failures=2)
    for _ in range(2):
        try:
            get(s)
        except requests.RequestException:
            pass
    with pytest.raises(CircuitOpen):
        get(s)


@pytest.mark.parametrize("answer", [200, 201, 301, 400, 401, 403, 404, 409, 422, 429])
def test_an_answer_that_is_not_a_server_failure_never_opens_it(clock, answer):
    net = Net(answer)
    s = guarded(net, clock, failures=2)
    for _ in range(10):
        assert get(s) == answer                                                       # (a 429 is a quota, a 404 a missing record: the service is up)
    assert net.sent == 10


@pytest.mark.parametrize("mistake", [requests.exceptions.InvalidURL("bad"), requests.exceptions.MissingSchema("no scheme"), requests.exceptions.InvalidHeader("h"),
                                     requests.exceptions.TooManyRedirects("loop")])
def test_a_mistake_in_our_own_request_is_not_the_service_being_down(clock, mistake):
    s = guarded(Net(mistake), clock, failures=2)
    for _ in range(5):
        with pytest.raises(requests.RequestException) as caught:
            get(s)
        assert not isinstance(caught.value, CircuitOpen)


def test_after_the_pause_one_request_goes_and_its_answer_decides(clock):
    net = Net(requests.ConnectionError("down"), requests.ConnectionError("down"), 200)
    s = guarded(net, clock, failures=2, pause=30)
    for _ in range(2):
        with pytest.raises(requests.ConnectionError):
            get(s)
    clock.now += 31
    assert get(s) == 200                                                              # the probe went out, and ERPNext was back
    assert get(s) == 200 and resilience.BREAKERS.get("erpnext").state == resilience.CLOSED
    assert net.sent == 4


def test_a_probe_that_fails_keeps_it_open(clock):
    net = Net(requests.ConnectionError("down"))
    s = guarded(net, clock, failures=1, pause=30)
    with pytest.raises(requests.ConnectionError):
        get(s)
    clock.now += 31
    with pytest.raises(requests.ConnectionError):
        get(s)                                                                         # the probe, which failed
    with pytest.raises(CircuitOpen):
        get(s)
    assert net.sent == 2


def test_a_session_is_guarded_once_and_two_sessions_share_the_dependencys_breaker(clock):
    net = Net(requests.ConnectionError("down"))
    s = guarded(net, clock, failures=2)
    assert guard_session(s, "erpnext") is s
    other = requests.Session()
    other.mount("http://", Net(200))
    guard_session(other, "erpnext")                                                   # e.g. a person's own sign-in session
    for _ in range(2):
        with pytest.raises(requests.ConnectionError):
            get(s)
    with pytest.raises(CircuitOpen):
        get(other)                                                                    # it learned from the other session's failures
    assert guard_session(object(), "erpnext") is not None                             # a test double without request() is left alone


def test_each_dependency_is_separate(clock):
    s_erp = guarded(Net(requests.ConnectionError("down")), clock, failures=1, name="erpnext")
    s_llm = requests.Session()
    s_llm.mount("http://", Net(200))
    guard_session(s_llm, "model:ollama")
    with pytest.raises(requests.ConnectionError):
        get(s_erp)
    with pytest.raises(CircuitOpen):
        get(s_erp)
    assert get(s_llm) == 200                                                          # a model that is up is not stopped by ERPNext being down


def test_the_session_helper_is_an_edge_and_the_rules_are_in_a_module_without_http():
    import ast
    import inspect
    from erp_agents.core import resilience as core
    imported = {n.names[0].name.split(".")[0] for n in ast.walk(ast.parse(inspect.getsource(core))) if isinstance(n, ast.Import)}
    imported |= {n.module.split(".")[0] for n in ast.walk(ast.parse(inspect.getsource(core))) if isinstance(n, ast.ImportFrom) and n.module}
    assert not imported & {"requests", "fastapi", "apscheduler", "sqlite3", "urllib3", "httpx"}
    assert httpguard.__doc__ and "edge" in httpguard.__doc__.lower()
