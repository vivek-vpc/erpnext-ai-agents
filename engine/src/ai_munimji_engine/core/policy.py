"""Settings models of the core: the system settings of the platform, the language-model assistant, and the AI agents (where the model runs, tokens, consent).

A pack's own settings live with the pack (packs/<name>/settings.py) and are registered with the SettingsStore; see core/packs.py.
"""
from __future__ import annotations

from typing import Literal

from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .common import untrusted


class SystemSettings(BaseModel):
    """Global, non-item settings of the platform: the time zone and how long an approval request waits. A pack adds its own fields by extending this class
    (the stock pack adds the schedule and the alert limits) and registering the result with `SettingsStore.extend_system`."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    timezone: str = Field("Asia/Kolkata", description="Time zone used for the schedule.")
    approval_expiry_hours: int = Field(72, ge=1, description="A request with no answer expires after this long (never auto-approved).")
    reminder_after_hours: int = Field(24, ge=1, description="Send one reminder if there is no answer after this long.")
    max_reminders: int = Field(2, ge=0, le=10, description="Most reminders per request.")

    @field_validator("timezone")
    @classmethod
    def _known_zone(cls, value: str) -> str:
        """A name the time zone database knows: anything else would stop the scheduler and every job that asks for today's date."""
        try:
            ZoneInfo(value)
        except (KeyError, ValueError, OSError):
            raise ValueError(f"'{untrusted(value, 40)}' is not a time zone name (for example Asia/Kolkata)") from None
        return value


LOCAL_HOSTS = ("localhost", "127.0.0.1", "::1", "[::1]")


class AssistantSettings(BaseModel):
    """The local language model that answers 'why?' questions. It only explains recorded data; it never decides."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    enabled: bool = Field(False, description="Let people ask questions about a request in the console. Off = the feature is hidden.")
    base_url: str = Field("http://127.0.0.1:11434", description="Address of the Ollama server. Must be on this machine unless 'allow_remote' is on.")
    model: str = Field("llama3.1", min_length=1, description="Ollama model name (as shown by 'ollama list').")
    timeout_seconds: int = Field(240, ge=10, le=1800, description="How long to wait for an answer. A laptop CPU can take a minute or more.")
    max_answer_chars: int = Field(900, ge=100, le=4000, description="Longer answers are rejected as unchecked.")
    allow_remote: bool = Field(False, description="Allow an Ollama server on another machine. Off keeps all data on this server.")

    @model_validator(mode="after")
    def _check(self) -> "AssistantSettings":
        from urllib.parse import urlparse
        url = urlparse(self.base_url)
        if url.scheme not in ("http", "https") or not url.hostname:
            raise ValueError("base_url must look like http://127.0.0.1:11434")
        if not self.allow_remote and url.hostname not in LOCAL_HOSTS:
            raise ValueError("base_url must be on this machine (localhost) unless allow_remote is switched on")
        return self


# Where the agents' language model can run. "ollama" is a model on this computer (or another one you run); the others are hosted services that bill per token.
# An API key is never a setting: it is read from the environment (the .env file) by config.py, so it cannot be typed into the screen or stored in the database.
AGENT_PROVIDERS = ("ollama", "anthropic", "openai", "gemini")
PROVIDER_LABELS = {"ollama": "Ollama (on this computer)", "anthropic": "Claude (Anthropic)", "openai": "OpenAI", "gemini": "Gemini (Google)"}
PROVIDER_URLS = {"ollama": "http://127.0.0.1:11434", "anthropic": "https://api.anthropic.com", "openai": "https://api.openai.com/v1", "gemini": "https://generativelanguage.googleapis.com"}
# Starting suggestions only: model names change. "Check connection" lists what the key can really use.
PROVIDER_MODELS = {"ollama": "llama3.1", "anthropic": "claude-haiku-5-5", "openai": "gpt-4o-mini", "gemini": "gemini-2.5-flash"}


class AgentSettings(BaseModel):
    """The AI agents (inventory and purchasing). A language model plans and words; the tools supply every number, and a person still approves every order.
    What is sent to the model is described in docs/PRIVACY.md."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    enabled: bool = Field(False, description="Let the agents run. Off = nothing is sent to any model by the agents.")
    provider: Literal["ollama", "anthropic", "openai", "gemini"] = Field("ollama", description="Where the language model runs: ollama (this computer), anthropic (Claude), openai or gemini (hosted services, billed per token).")
    base_url: str = Field("", description="Address of the model server. Empty = the provider's own address (for Ollama http://127.0.0.1:11434). Fill it in for an Ollama on another machine or an OpenAI-compatible service.")
    model: str = Field("", description="The model's name. Empty = a suggested one for the provider. For Ollama it must support tool calling (see 'ollama list').")
    price_input_per_million: float = Field(0.0, ge=0, le=100000, description="What your provider charges per million input tokens, in your own currency (from its price list). With a price set the screen shows an estimated cost. 0 = do not estimate.")
    price_output_per_million: float = Field(0.0, ge=0, le=100000, description="What your provider charges per million output tokens, in your own currency. 0 = do not estimate.")
    monthly_token_limit: int = Field(0, ge=0, le=10_000_000_000, description="Most tokens (input and output) the agents may use on a hosted model in a calendar month. At the limit they stop using it and the plain rules answer instead. 0 = no limit.")
    timeout_seconds: int = Field(300, ge=30, le=1800, description="How long to wait for ONE model reply. A laptop CPU can take a minute or more.")
    tools_url: str = Field("", description="Use another MCP server's tools instead of the built-in ones (an address ending in /mcp). Its access key is read from the environment variable ERP_AGENTS_MCP_TOKEN. Empty = the built-in tools.")
    max_output_tokens: int = Field(400, ge=64, le=4000, description="Longest reply the model may write. A small model can run on without ever closing its answer; this stops it (a reply cut short is rejected like any other bad one).")
    max_steps: int = Field(6, ge=1, le=12, description="Most model replies in one task. Each reply may call tools; the task stops here even if the model wants more.")
    max_tool_calls: int = Field(10, ge=1, le=30, description="Most tool calls in one task.")
    allow_remote: bool = Field(False, description="Allow a model server on another machine or a hosted model. Off keeps everything the agents read on this server.")
    keep_traces_days: int = Field(30, ge=1, le=365, description="How long the record of what was sent to the model and what it answered is kept.")
    second_opinion: bool = Field(False, description="The purchasing agent reads each new order request and sends a short second opinion with it. It never changes the request.")
    daily_brief_cron: str = Field("", description="When the inventory agent writes its brief (cron, for example '0 9 * * *'). Empty = only when asked.")
    send_brief: bool = Field(False, description="Send the inventory brief to the approvers on Telegram and WhatsApp as a plain message.")

    def effective_base_url(self) -> str:
        return (self.base_url.strip() or PROVIDER_URLS[self.provider]).rstrip("/")

    def effective_model(self) -> str:
        return self.model.strip() or PROVIDER_MODELS[self.provider]

    @model_validator(mode="after")
    def _check(self) -> "AgentSettings":
        from urllib.parse import urlparse
        url = urlparse(self.effective_base_url())
        if url.scheme not in ("http", "https") or not url.hostname:
            raise ValueError("base_url must look like http://127.0.0.1:11434")
        if not self.allow_remote and url.hostname not in LOCAL_HOSTS:
            if self.provider != "ollama" and not self.base_url.strip():
                raise ValueError(f"{PROVIDER_LABELS[self.provider]} is a hosted service: what the agents read is sent to it. Switch on allow_remote to confirm that, as described in the privacy notes")
            raise ValueError("base_url must be on this machine (localhost) unless allow_remote is switched on")
        if self.provider != "ollama" and self.base_url.strip() and url.scheme != "https" and url.hostname not in LOCAL_HOSTS:
            raise ValueError("a hosted service must be reached over https")
        if self.tools_url.strip():
            tools = urlparse(self.tools_url.strip())
            if tools.scheme not in ("http", "https") or not tools.hostname:
                raise ValueError("tools_url must look like http://127.0.0.1:8100/mcp")
            if not self.allow_remote and tools.hostname not in LOCAL_HOSTS:
                raise ValueError("tools_url must be on this machine (localhost) unless allow_remote is switched on")
        if self.daily_brief_cron.strip() and len(self.daily_brief_cron.split()) != 5:
            raise ValueError("daily_brief_cron must be a cron expression with 5 fields, for example '0 9 * * *'")
        return self
