"""Packs: switchable areas of the application on top of one shared core.

The core owns what every area needs: ERPNext access, the approvals and their channels, sign-in and roles, settings with their audit trail, the tool registry and the MCP server,
the AI agent runtime with the checks on its answers, privacy, and the operating tools. A **pack** adds one area (stock and procurement today; sales and finance are empty
skeletons) by declaring a manifest and implementing the hooks of `Pack`.

Rules that make the split real (and that tests enforce, tests/test_pack_boundaries.py):

  * a pack imports the core and itself, never another pack: what two areas share goes through the core's tool registry, the approvals, the work orders, or a tool call;
  * the core never imports a pack: it finds packs by directory (`discover`), loads an enabled one by name (`PackRegistry.load`) and talks to it only through `Pack`;
  * role checks, the order gate and the verification of answers live in the core and apply to every pack's tools: a pack declares its tools and who may use them, and the core
    enforces it on every call, whichever way the call arrives (REST, MCP, an agent);
  * a pack that is switched off does nothing: its tools refuse, its routes answer 404, its scheduled work returns at once.

A pack's manifest (`manifest.yaml` beside its code) is read and checked WITHOUT importing the pack, so a disabled or broken pack never runs. What it declares is also checked
against what the pack really registers when it starts: a tool that is registered but not declared (or the other way round) stops the application, so the manifest can be trusted.
"""
from __future__ import annotations

import importlib
import importlib.util
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

log = logging.getLogger(__name__)

PACKS_PACKAGE = "erp_agents.packs"
MANIFEST_FILE = "manifest.yaml"
NAME_RE = re.compile(r"^[a-z][a-z0-9_]{1,39}$")
TOOL_RE = re.compile(r"^[a-z][a-z0-9_]{1,59}$")
KIND_RE = re.compile(r"^[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*$")
VERSION_RE = re.compile(r"^\d+\.\d+\.\d+([.\-+][0-9A-Za-z.\-+]+)?$")
ROLES = ("viewer", "approver", "admin")             # what the core's role checks understand
EFFECTS = ("read", "prepare")                       # a tool reads, or prepares an approval request for a person to answer: none orders, approves, pays or changes a setting
STATUSES = ("active", "skeleton")


class PackError(ValueError):
    """A manifest or a pack is not acceptable. The message says which pack and what to fix."""


# ---------------------------------------------------------------------------------------------------------------------- the manifest
@dataclass(frozen=True)
class ToolDecl:
    name: str
    role: str                       # who may call it: viewer | approver | admin (the core checks this on every call)
    effect: str                     # read | prepare


@dataclass(frozen=True)
class ApprovalDecl:
    kind: str                       # the kind of approval request the pack sends, e.g. "procurement.po"
    actions: tuple[str, ...]        # what a person can press on it
    role: str                       # who may answer
    description: str
    creates: str = ""               # what answering it may create in the ERP ("a draft Purchase Order"); empty = nothing


@dataclass(frozen=True)
class SettingDecl:
    section: str                    # the settings section (or "system" for fields added to the system settings)
    description: str


@dataclass(frozen=True)
class PackManifest:
    name: str
    version: str
    title: str
    summary: str
    status: str = "active"                                      # active | skeleton (an empty pack: no logic, nothing declared)
    requires_core: str = ""                                     # informational: the core version range this pack was written for
    enabled_by_default: bool = True
    depends_on: tuple[str, ...] = ()                            # must stay empty: packs depend only on the core, never on each other
    tools: tuple[ToolDecl, ...] = ()
    erp_roles: tuple[str, ...] = ()                             # ERPNext roles the pack needs (for the service account and for the people who use it)
    erp_permissions: tuple[tuple[str, str], ...] = ()           # (DocType, permission) pairs the pack needs the service account to hold
    erp_look: tuple[tuple[str, str], ...] = ()                  # a person who holds ANY of these in ERPNext may look at what the pack shows (sign-in capability "look")
    erp_decide: tuple[tuple[str, str], ...] = ()                # a person who holds ANY of these in ERPNext may answer the pack's approval requests (capability "decide")
    approvals: tuple[ApprovalDecl, ...] = ()
    settings: tuple[SettingDecl, ...] = ()
    sends_to_model: tuple[str, ...] = ()                        # plain-language list of what an AI agent of this pack may send to a language model (the privacy statement)

    @property
    def tool_names(self) -> tuple[str, ...]:
        return tuple(t.name for t in self.tools)

    def describe(self) -> dict[str, Any]:
        return {"name": self.name, "version": self.version, "title": self.title, "summary": self.summary, "status": self.status, "enabled_by_default": self.enabled_by_default,
                "tools": [{"name": t.name, "role": t.role, "effect": t.effect} for t in self.tools], "erp_roles": list(self.erp_roles),
                "approvals": [{"kind": a.kind, "actions": list(a.actions), "role": a.role, "description": a.description, "creates": a.creates} for a in self.approvals],
                "settings": [{"section": s.section, "description": s.description} for s in self.settings]}


def _strings(data: dict[str, Any], key: str, where: str) -> tuple[str, ...]:
    value = data.get(key) or []
    if not isinstance(value, list) or not all(isinstance(x, str) and x.strip() for x in value):
        raise PackError(f"{where}: '{key}' must be a list of non-empty strings")
    return tuple(x.strip() for x in value)


def _entries(data: dict[str, Any], key: str, where: str) -> list[dict[str, Any]]:
    value = data.get(key) or []
    if not isinstance(value, list) or not all(isinstance(x, dict) for x in value):
        raise PackError(f"{where}: '{key}' must be a list of entries")
    return value


def parse_manifest(data: Any, where: str, directory_name: Optional[str] = None) -> PackManifest:
    """Validate a manifest read from YAML. `where` names the file for the error messages; `directory_name` is the folder the manifest sits in (it must match `name`)."""
    if not isinstance(data, dict):
        raise PackError(f"{where}: the manifest must be a mapping")
    unknown = sorted(set(data) - {"name", "version", "title", "summary", "status", "requires_core", "enabled_by_default", "depends_on", "tools", "erp_roles", "erp_permissions",
                                  "approvals", "settings", "sends_to_model", "erp_look", "erp_decide"})
    if unknown:
        raise PackError(f"{where}: unknown manifest key(s): {', '.join(unknown)}")
    for key in ("name", "version", "title", "summary"):
        if not isinstance(data.get(key), str) or not data[key].strip():
            raise PackError(f"{where}: '{key}' is required")
    name = data["name"].strip()
    if not NAME_RE.match(name):
        raise PackError(f"{where}: name {name!r} must be lower-case letters, digits and underscores, starting with a letter")
    if directory_name is not None and directory_name != name:
        raise PackError(f"{where}: the manifest says name {name!r} but the folder is {directory_name!r}; they must match")
    if not VERSION_RE.match(data["version"].strip()):
        raise PackError(f"{where}: version {data['version']!r} must look like 1.2.3")
    status = str(data.get("status", "active")).strip()
    if status not in STATUSES:
        raise PackError(f"{where}: status must be one of {', '.join(STATUSES)}")
    depends = _strings(data, "depends_on", where)
    if depends:
        raise PackError(f"{where}: a pack depends only on the core, never on another pack (depends_on lists {', '.join(depends)}); "
                        "what two packs share goes through the core's tool registry, approvals or work orders")
    enabled = data.get("enabled_by_default", True)
    if not isinstance(enabled, bool):
        raise PackError(f"{where}: enabled_by_default must be true or false")
    tools = []
    for entry in _entries(data, "tools", where):
        extra = sorted(set(entry) - {"name", "role", "effect"})
        if extra or not {"name", "role", "effect"} <= set(entry):
            raise PackError(f"{where}: a tool entry needs exactly name, role and effect (got {', '.join(sorted(entry)) or 'nothing'})")
        if not TOOL_RE.match(str(entry["name"])):
            raise PackError(f"{where}: tool name {entry['name']!r} is not valid")
        if entry["role"] not in ROLES:
            raise PackError(f"{where}: tool {entry['name']}: role must be one of {', '.join(ROLES)}")
        if entry["effect"] not in EFFECTS:
            raise PackError(f"{where}: tool {entry['name']}: effect must be one of {', '.join(EFFECTS)} (no tool orders, approves, pays or changes a setting)")
        tools.append(ToolDecl(str(entry["name"]), entry["role"], entry["effect"]))
    if len({t.name for t in tools}) != len(tools):
        raise PackError(f"{where}: a tool is declared twice")
    approvals = []
    for entry in _entries(data, "approvals", where):
        if not {"kind", "actions", "role", "description"} <= set(entry) or set(entry) - {"kind", "actions", "role", "description", "creates"}:
            raise PackError(f"{where}: an approval entry needs kind, actions, role and description (and may have creates)")
        if not KIND_RE.match(str(entry["kind"])):
            raise PackError(f"{where}: approval kind {entry['kind']!r} must look like area.thing")
        if entry["role"] not in ROLES:
            raise PackError(f"{where}: approval {entry['kind']}: role must be one of {', '.join(ROLES)}")
        actions = entry["actions"]
        if not isinstance(actions, list) or not actions or not all(isinstance(a, str) and a for a in actions):
            raise PackError(f"{where}: approval {entry['kind']}: actions must be a non-empty list of names")
        approvals.append(ApprovalDecl(str(entry["kind"]), tuple(actions), entry["role"], str(entry["description"]), str(entry.get("creates") or "")))
    if len({a.kind for a in approvals}) != len(approvals):
        raise PackError(f"{where}: an approval kind is declared twice")
    settings = []
    for entry in _entries(data, "settings", where):
        if set(entry) != {"section", "description"}:
            raise PackError(f"{where}: a settings entry needs exactly section and description")
        settings.append(SettingDecl(str(entry["section"]), str(entry["description"])))
    def pairs(key: str) -> tuple[tuple[str, str], ...]:
        out = []
        for pair in data.get(key) or []:
            if not (isinstance(pair, list) and len(pair) == 2 and all(isinstance(x, str) and x for x in pair)):
                raise PackError(f"{where}: {key} must be a list of [DocType, permission] pairs")
            out.append((pair[0], pair[1]))
        return tuple(out)
    manifest = PackManifest(name=name, version=data["version"].strip(), title=data["title"].strip(), summary=data["summary"].strip(), status=status,
                            requires_core=str(data.get("requires_core") or ""), enabled_by_default=enabled, depends_on=(), tools=tuple(tools),
                            erp_roles=_strings(data, "erp_roles", where), erp_permissions=pairs("erp_permissions"), erp_look=pairs("erp_look"),
                            erp_decide=pairs("erp_decide"), approvals=tuple(approvals), settings=tuple(settings),
                            sends_to_model=_strings(data, "sends_to_model", where))
    if status == "skeleton" and (manifest.tools or manifest.approvals or manifest.settings or manifest.erp_roles or manifest.erp_permissions or manifest.erp_look
                                  or manifest.erp_decide or manifest.sends_to_model):
        raise PackError(f"{where}: a skeleton pack has no logic, so it declares no tools, approvals, settings, roles or permissions")
    return manifest


def load_manifest(path: Path) -> PackManifest:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise PackError(f"{path}: cannot be read as YAML ({str(exc)[:120]})") from exc
    return parse_manifest(data, str(path), directory_name=path.parent.name)


# ---------------------------------------------------------------------------------------------------------------------- the settings switch
class PackSettings(BaseModel):
    """Which packs are on. A pack not listed here follows its manifest's `enabled_by_default`. Switching one OFF takes effect at once (its tools refuse, its routes answer 404,
    its scheduled work returns); switching one ON that was off when the application started needs a restart (its code was not loaded)."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    enabled: dict[str, bool] = Field(default_factory=dict, description="Pack name -> on or off. A pack that is not listed follows its default (stock_procurement is on; sales and finance are empty skeletons).")

    @field_validator("enabled", mode="after")
    @classmethod
    def _names(cls, value: dict[str, bool]) -> dict[str, bool]:
        bad = [k for k in value if not NAME_RE.match(k)]
        if bad:
            raise ValueError(f"not a pack name: {', '.join(bad[:3])}")
        return value


# ---------------------------------------------------------------------------------------------------------------------- what a pack implements
@dataclass(frozen=True)
class CliCommand:
    name: str
    help: str
    configure: Callable[[Any], None]            # adds the command's arguments to its argparse sub-parser
    run: Callable[[Any], int]                   # the handler: (argparse namespace) -> exit code


@dataclass
class ToolDef:
    """A tool as a pack registers it: the specification (name, description, parameters, role, effect: a `core.toolkit.ToolSpec`) and the function that runs it."""
    spec: Any
    handler: Callable[..., dict[str, Any]]


class Pack:
    """Base class of a pack's entry object (`PACK` in `<pack>/pack.py`). Every hook has a do-nothing default, so a pack implements only what it adds."""

    manifest: PackManifest

    def setup(self, app: Any) -> None:
        """Called once at start-up, after the core is built and before anything is served. Build the pack's services and register them (`app.services`), its settings sections,
        its database schema, its approval kinds and handlers, its tools (`app.toolkit.register`), and anything else it adds to the core's registries."""

    def routes(self, router: Any, ctx: Any, deps: Any) -> None:
        """Add REST routes to `router` (a FastAPI router that answers 404 when this pack is switched off, after the usual sign-in check). `ctx` is the application; `deps`
        gives `require(role)`, the core's role check, and `MAX_LIST_LIMIT`."""

    def mcp_tools(self, server: Any, run: Callable[..., Any]) -> None:
        """Register the pack's tools on an MCP server with typed signatures, each calling `run(name, args)` (which applies the core's role checks)."""

    def cli_commands(self) -> list["CliCommand"]:
        """Command-line commands this pack adds (`erp-agents <name> ...`). A handler gets the argparse namespace and returns the exit code; it should start with
        `core.cli.pack_app(args, <pack name>)`, which builds the application and explains when the pack is switched off."""
        return []

    def agent_actions(self) -> dict[str, Callable[[Any, Any], int]]:
        """Extra actions of `erp-agents agents <action>` (the stock pack adds brief, review and ask). A handler gets (the argparse namespace, the application)."""
        return {}

    def jobs(self, scheduler: Any) -> None:
        """Add scheduled work to the core's scheduler (`scheduler.sched`, `scheduler.app`); a job must return at once when the pack is switched off."""

    def on_settings_change(self, scheduler: Any, scope: str, key: str) -> None:
        """A setting was changed by an administrator: reschedule anything that depends on it."""

    def mcp_instructions(self) -> str:
        """A sentence or two for the MCP server's instructions: what this pack's tools are for and how to use the one that prepares a request."""
        return ""

    def web_dir(self) -> Optional[Path]:
        """A folder with an index.html the core serves as the console, if this pack has screens."""
        return None


# ---------------------------------------------------------------------------------------------------------------------- finding and loading packs
class PackRegistry:
    """The packs found in `erp_agents/packs/*/manifest.yaml`, which of them are on, and the ones that were loaded."""

    def __init__(self, manifests: dict[str, PackManifest], roots: dict[str, Path]) -> None:
        self.manifests, self.roots = manifests, roots
        self.loaded: dict[str, Pack] = {}
        self._settings: Optional[Callable[[], PackSettings]] = None

    # -- discovery
    @classmethod
    def discover(cls, package: str = PACKS_PACKAGE, directory: Optional[Path] = None) -> "PackRegistry":
        """Read every manifest under the packs package. Nothing is imported: a pack that is off or broken never runs. A bad manifest stops the application with its file named."""
        if directory is None:
            spec = importlib.util.find_spec(package)
            if spec is None or not spec.submodule_search_locations:
                return cls({}, {})
            directory = Path(next(iter(spec.submodule_search_locations)))
        manifests: dict[str, PackManifest] = {}
        roots: dict[str, Path] = {}
        for child in sorted(p for p in directory.iterdir() if p.is_dir() and not p.name.startswith(("_", "."))):
            manifest_path = child / MANIFEST_FILE
            if not manifest_path.is_file():
                continue
            manifest = load_manifest(manifest_path)
            if manifest.name in manifests:
                raise PackError(f"{manifest_path}: pack {manifest.name!r} is already declared in {roots[manifest.name]}")
            manifests[manifest.name] = manifest
            roots[manifest.name] = child
        seen: dict[str, str] = {}
        for m in manifests.values():
            for t in m.tools:
                if t.name in seen:
                    raise PackError(f"tool {t.name!r} is declared by both {seen[t.name]} and {m.name}: tool names are shared across packs")
                seen[t.name] = m.name
        return cls(manifests, roots)

    # -- which are on
    def bind_settings(self, read: Callable[[], PackSettings]) -> None:
        self._settings = read

    def is_enabled(self, name: str) -> bool:
        """Live: reads the settings each time, so switching a pack off needs no restart."""
        manifest = self.manifests.get(name)
        if manifest is None:
            return False
        flags = self._settings().enabled if self._settings is not None else {}
        return flags.get(name, manifest.enabled_by_default)

    def enabled_names(self) -> list[str]:
        return [n for n in self.manifests if self.is_enabled(n)]

    def unknown_in_settings(self) -> list[str]:
        """Names in the settings that match no installed pack (a typo, or a pack that was removed)."""
        flags = self._settings().enabled if self._settings is not None else {}
        return sorted(set(flags) - set(self.manifests))

    # -- loading
    def load(self, name: str) -> Pack:
        """Import an enabled pack's code (`<package>.<name>.pack`, attribute PACK) and check it is the pack its manifest describes."""
        if name in self.loaded:
            return self.loaded[name]
        if name not in self.manifests:
            raise PackError(f"there is no pack named {name!r}")
        manifest = self.manifests[name]
        if manifest.status == "skeleton":
            raise PackError(f"pack {name!r} is a skeleton: it has no code to load")
        module = importlib.import_module(f"{PACKS_PACKAGE}.{name}.pack")
        pack = getattr(module, "PACK", None)
        if not isinstance(pack, Pack):
            raise PackError(f"{PACKS_PACKAGE}.{name}.pack must define PACK, an instance of core.packs.Pack")
        if pack.manifest != manifest:
            raise PackError(f"pack {name!r}: the manifest in the code is not the one in {MANIFEST_FILE}; they must be the same")
        self.loaded[name] = pack
        return pack

    def check_registered(self, name: str, registered_tools: dict[str, tuple[str, str]]) -> None:
        """After a pack's setup: the tools it registered (name -> (role, effect)) must be exactly the tools its manifest declares, with the roles and effects declared."""
        declared = {t.name: (t.role, t.effect) for t in self.manifests[name].tools}
        if registered_tools != declared:
            missing = sorted(set(declared) - set(registered_tools))
            extra = sorted(set(registered_tools) - set(declared))
            differ = sorted(n for n in set(declared) & set(registered_tools) if declared[n] != registered_tools[n])
            parts = ([f"declared but not registered: {', '.join(missing)}"] if missing else []) + ([f"registered but not declared: {', '.join(extra)}"] if extra else []) + \
                    ([f"role or effect differs: {', '.join(differ)}"] if differ else [])
            raise PackError(f"pack {name!r}: its tools do not match its manifest ({'; '.join(parts)})")

    def erp_permissions(self) -> list[tuple[str, str]]:
        """What the ERPNext service account must hold for the enabled packs: the union of their manifests' erp_permissions, in a stable order."""
        return list(dict.fromkeys(p for n in self.enabled_names() for p in self.manifests[n].erp_permissions))

    def erp_capabilities(self) -> dict[str, list[tuple[str, str]]]:
        """What a person needs in ERPNext for the sign-in capabilities, from the enabled packs: 'look' and 'decide' (holding ANY of the listed permissions is enough)."""
        names = self.enabled_names()
        return {"look": list(dict.fromkeys(p for n in names for p in self.manifests[n].erp_look)), "decide": list(dict.fromkeys(p for n in names for p in self.manifests[n].erp_decide))}

    def privacy_lines(self) -> list[str]:
        """What the enabled packs' agents may send to a language model, in plain words, for the privacy statement."""
        return [line for n in self.enabled_names() for line in self.manifests[n].sends_to_model]

    def describe(self) -> list[dict[str, Any]]:
        return [{**m.describe(), "enabled": self.is_enabled(m.name), "loaded": m.name in self.loaded} for m in self.manifests.values()]
