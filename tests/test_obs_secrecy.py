"""No token, key or phone number reaches the logs (Phase 2 of the hardening brief).

Every scenario runs REAL code (the channels, the ERPNext client, the model clients, the agents) with sentinel secrets in the configuration, makes the outside world fail in a way
that quotes them (a network error that names the address, a server that echoes the credentials back in an error page), and then reads every line that was logged, in the plain
and in the JSON format. A secret must not appear in any of them. The same scenarios run with the scrubber switched off (`OBS_RAW=1 pytest tests/test_obs_secrecy.py`) show which
paths leak without it."""
import io
import json
import logging
import os
from contextlib import contextmanager

import pytest
import requests

from conftest import FakeHTTP, FakeResponse
from erp_agents.core.agents.llm import AnthropicToolClient
from erp_agents.core.approvals.channels.telegram import TelegramChannel
from erp_agents.core.approvals.channels.whatsapp import WhatsAppChannel
from erp_agents.core.erp.erpnext import ERPNextClient
from erp_agents.core.erp.mock import MockERP

RAW = bool(os.environ.get("OBS_RAW"))
if not RAW:
    from erp_agents.core import obs

ACCESS_KEY = "AK-sentinel-4f9c2a71b3d84e6f90ab"
HASH_KEY = "hash-sentinel-0123456789abcdef0123456789"
TELEGRAM_TOKEN = "123456789" + ":" + "AAH-sentinelTELEGRAMtoken_abcdefghijklmnopq"            # built in pieces: the repository's own secret scan (ci.yml) must not trip on a test's sentinel
WHATSAPP_TOKEN = "EAA" + "sentinelWhatsAppAccessTokenxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
ERP_KEY, ERP_SECRET = "erpkey-sentinel-77aa91", "erpsecret-sentinel-55bb13"
MODEL_KEY = "sk-ant-sentinel-0123456789abcdefghijklmn"
OWNER_PHONE = "919812345678"
VENDOR_PHONE = "919876543210"
VENDOR_PHONE_SPACED = "+91 98765 43210"
SECRETS = {"access key": ACCESS_KEY, "hash key": HASH_KEY, "telegram token": TELEGRAM_TOKEN, "whatsapp token": WHATSAPP_TOKEN, "erp key": ERP_KEY, "erp secret": ERP_SECRET,
           "model key": MODEL_KEY, "owner's phone": OWNER_PHONE, "vendor's phone": VENDOR_PHONE, "vendor's phone, spaced": VENDOR_PHONE_SPACED}


class Down(FakeHTTP):
    """The network is down, and the error says where it was going and what it carried, the way a real one does."""

    def __init__(self, detail):
        super().__init__()
        self.detail = detail

    def post(self, url, json=None, headers=None, timeout=None):
        self.calls.append((url, json, headers))
        raise requests.ConnectionError(f"HTTPSConnectionPool(host='api.example.test', port=443): Max retries exceeded with url: {url} ({self.detail})")

    get = post


class EchoingServer:
    """An ERPNext (or model) server that answers with an error page that quotes the request's credentials back."""

    def __init__(self, body):
        self.headers = {}
        self.body = body
        self.calls = []

    def _answer(self, *a, **k):
        self.calls.append(a)
        return FakeResponse({"exc": self.body}, 500)

    get = post = put = delete = request = _answer


@contextmanager
def logged(fmt="text"):
    """Every line logged inside the block, as the configured handler would write it."""
    root = logging.getLogger()
    saved, level = root.handlers[:], root.level
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    if RAW:
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    else:
        handler = obs.make_handler(buf, fmt)
    root.handlers = [handler]
    root.setLevel(logging.DEBUG)                             # setLevel, not an assignment: it also clears the loggers' cache of "is this level enabled"
    try:
        yield buf
    finally:
        root.handlers = saved
        root.setLevel(level)


def leaks(text):
    return sorted(name for name, secret in SECRETS.items() if secret in text or secret.replace(" ", "") in text.replace(" ", ""))


def app_with(make_app, clock, **kw):
    cfg = {"auth": {"tokens": [{"user": "owner", "token": ACCESS_KEY, "roles": ["admin", "approver"]}]},
           "approvals": {"strategy": "all", "recipients": [{"id": "owner", "name": "Owner", "roles": ["admin", "approver"],
                                                              "channels": [{"type": "whatsapp", "address": OWNER_PHONE}, {"type": "telegram", "address": "555"}]}]}}
    return make_app(cfg=cfg, **kw)


def whatsapp_down():
    return WhatsAppChannel(WHATSAPP_TOKEN, "PNID", http=Down(f"to {OWNER_PHONE}, vendor {VENDOR_PHONE_SPACED}, Authorization: Bearer {WHATSAPP_TOKEN}"))


# ------------------------------------------------------------------ the scenarios: each makes real code fail with a secret in the error
def sc_whatsapp_delivery_fails(make_app, clock):
    app = app_with(make_app, clock, erp=MockERP(today=clock().date()), channels={"whatsapp": whatsapp_down()})
    app.inventory.run()                                     # alerts go to the owner on WhatsApp; the send fails; the failure is logged


def sc_telegram_delivery_fails(make_app, clock):
    http = Down(f"bot token in the address: /bot{TELEGRAM_TOKEN}/sendMessage")
    app = app_with(make_app, clock, erp=MockERP(today=clock().date()), channels={"telegram": TelegramChannel(TELEGRAM_TOKEN, http=http)})
    app.inventory.run()


def sc_erpnext_answers_with_the_credentials_in_its_error(make_app, clock):
    server = EchoingServer(f"Traceback ... PermissionError: token {ERP_KEY}:{ERP_SECRET} may not read Item (requested by {VENDOR_PHONE})")
    erp = ERPNextClient("https://erp.example.test", ERP_KEY, ERP_SECRET, session=server)
    app = app_with(make_app, clock, erp=erp)
    app.inventory.run()                                     # the run fails and says why


def sc_a_hosted_model_fails_while_an_agent_runs(make_app, clock):
    pytest.importorskip("mcp")
    server = EchoingServer(f"invalid x-api-key {MODEL_KEY}")
    app = app_with(make_app, clock, erp=MockERP(today=clock().date()))
    from conftest import OWNER
    app.settings.update_section(OWNER, "agents", {"enabled": True, "provider": "anthropic", "allow_remote": True})
    app.agents.client_factory = lambda cfg: AnthropicToolClient("https://api.anthropic.com", "claude-haiku-5-5", MODEL_KEY, session=server)
    app.agents.ask("How much BOLT-M8 do we have?", OWNER)


def sc_a_library_logs_an_address_with_a_credential(make_app, clock):
    app_with(make_app, clock, erp=MockERP(today=clock().date()))
    logging.getLogger("urllib3.connectionpool").debug("Starting new HTTPS connection: https://api.example.test/v1/messages?api_key=%s&to=%s", ACCESS_KEY, VENDOR_PHONE)


def sc_an_exception_message_holds_a_secret(make_app, clock):
    app_with(make_app, clock, erp=MockERP(today=clock().date()))
    try:
        raise RuntimeError(f"cannot sign {WHATSAPP_TOKEN} for {OWNER_PHONE}")
    except RuntimeError:
        logging.getLogger("erp_agents.core.scheduler").exception("approval maintenance failed")


SCENARIOS = [sc_whatsapp_delivery_fails, sc_telegram_delivery_fails, sc_erpnext_answers_with_the_credentials_in_its_error, sc_a_hosted_model_fails_while_an_agent_runs,
             sc_a_library_logs_an_address_with_a_credential, sc_an_exception_message_holds_a_secret]


@pytest.mark.parametrize("fmt", ["text", "json"])
@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda s: s.__name__)
def test_no_token_key_or_phone_number_reaches_the_logs(scenario, fmt, make_app, clock, monkeypatch):
    monkeypatch.setenv("ERP_AGENTS_HASH_KEY", HASH_KEY)
    if not RAW:
        obs.SCRUBBER.forget_all()                            # what other tests registered is not what this one is about
    with logged(fmt) as buf:
        scenario(make_app, clock)
    text = buf.getvalue()
    assert text.strip(), "the scenario logged nothing, so it proves nothing"
    assert leaks(text) == [], f"{scenario.__name__} leaked {leaks(text)} into the {fmt} log:\n{text[:1500]}"
    if fmt == "json":
        for line in text.strip().splitlines():
            assert isinstance(json.loads(line), dict)                     # every line is one JSON object
