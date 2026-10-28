"""settings.yaml loading (with ${ENV} expansion so secrets stay out of the file)."""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Optional

import yaml

_ENV = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")

DEFAULT_CONFIG: dict[str, Any] = {
    "database": {"path": "./data/erp_agents.db"},
    "erp": {"type": "mock"},
    "policy": {},
    "system": {},
    "channels": {"console": {"enabled": True}, "inbox": {"enabled": True}},
    "approvals": {
        "strategy": "all",
        "recipients": [{"id": "owner", "name": "Owner", "roles": ["admin", "approver"],
                        "channels": [{"type": "console"}, {"type": "inbox"}]}],
    },
    "auth": {"tokens": []},
    "server": {"host": "127.0.0.1", "port": 8100},
}


# The environment variables that hold a hosted model's API key (the .env file is read into the environment at start). A key is never a setting: it cannot be typed into the
# console or stored in the database, and nothing outside this file reads the environment.
# The name made for this application comes first, so it can have a key of its own (its own usage on the provider's bill) even when the usual variable is set for another tool.
LLM_KEY_ENV = {"anthropic": ("ERP_AGENTS_ANTHROPIC_API_KEY", "ANTHROPIC_API_KEY"), "openai": ("ERP_AGENTS_OPENAI_API_KEY", "OPENAI_API_KEY"),
               "gemini": ("ERP_AGENTS_GEMINI_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY")}


def _found_keys() -> dict[str, tuple[str, str]]:
    found: dict[str, tuple[str, str]] = {}
    for provider, names in LLM_KEY_ENV.items():
        for name in names:
            value = os.environ.get(name, "").strip()
            if value:
                found[provider] = (name, value)
                break
    return found


def llm_keys_from_env() -> dict[str, str]:
    return {provider: value for provider, (_name, value) in _found_keys().items()}


def llm_key_sources_from_env() -> dict[str, str]:
    """Which variable each key was found in (the NAME, so the screen can say where it comes from; never the key)."""
    return {provider: name for provider, (name, _value) in _found_keys().items()}


HASH_KEY_ENV = "ERP_AGENTS_HASH_KEY"
MIN_HASH_KEY_LENGTH = 32


def hash_key() -> Optional[bytes]:
    """The secret that keys the hash used to recognise a vendor's phone number or address when they reply (finding F-8). It comes from the environment (the .env file is read
    into it at start), never from a setting or the database. None when it is missing or shorter than 32 characters: then nothing is hashed and a reply cannot be matched."""
    value = os.environ.get(HASH_KEY_ENV, "").strip()
    return value.encode("utf-8") if len(value) >= MIN_HASH_KEY_LENGTH else None


def hash_key_state() -> str:
    """'ok', 'missing' or 'short', for the start-up log and `doctor` (the key itself is never shown)."""
    value = os.environ.get(HASH_KEY_ENV, "").strip()
    return "missing" if not value else "ok" if len(value) >= MIN_HASH_KEY_LENGTH else "short"


def with_secrets(cfg: dict[str, Any]) -> dict[str, Any]:
    """Adds what must come from the environment: the hosted-model keys, and the key for another MCP server's tools."""
    cfg["llm_keys"] = llm_keys_from_env()
    cfg["llm_key_sources"] = llm_key_sources_from_env()
    token = os.environ.get("ERP_AGENTS_MCP_TOKEN", "").strip()
    if token:
        cfg["mcp"] = {**(cfg.get("mcp") or {}), "client_token": token}
    return cfg


def expand_env(value: Any) -> Any:
    if isinstance(value, str):
        return _ENV.sub(lambda m: os.environ.get(m.group(1), m.group(2) or ""), value)
    if isinstance(value, list):
        return [expand_env(v) for v in value]
    if isinstance(value, dict):
        return {k: expand_env(v) for k, v in value.items()}
    return value


def _merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in over.items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def load_dotenv(path: Path) -> list[str]:
    """Read KEY=VALUE lines from a .env file into os.environ. Variables already set in the real environment
    win, so a value typed in the shell overrides the file. Returns the names it set. No quoting magic beyond
    stripping one pair of matching quotes; `#` starts a comment only at the beginning of a line."""
    if not path.is_file():
        return []
    loaded: list[str] = []
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip().removeprefix("export ").strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key and value and key not in os.environ:      # empty = "not set", so ${NAME:-default} still applies
            os.environ[key] = value
            loaded.append(key)
    return loaded


def load_config(path: Optional[str] = None) -> dict[str, Any]:
    path = path or os.environ.get("ERP_AGENTS_CONFIG", "config/settings.yaml")
    p = Path(path)
    # Secrets live in .env (git-ignored): the project root next to config/, else the current directory.
    for env_file in dict.fromkeys([p.resolve().parent.parent / ".env", Path.cwd() / ".env"]):
        load_dotenv(env_file)
    if not p.exists():
        return with_secrets(expand_env(DEFAULT_CONFIG))
    raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    return with_secrets(expand_env(_merge(DEFAULT_CONFIG, raw)))
