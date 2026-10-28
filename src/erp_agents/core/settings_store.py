"""Admin-editable settings with scope inheritance and a full audit trail.

The store itself knows only the core's settings (the system settings of the platform, the assistant, the AI agents, the switch for packs). A pack REGISTERS what it adds:
sections (`register_section`), the system-settings fields it extends (`extend_system`) and its thresholds that can be overridden per item group and per item (`register_scoped`).
Everything registered is validated, audited and editable the same way as the core's own.
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Optional

from pydantic import ValidationError

from .common import Actor, Clock, iso, utcnow
from .packs import PackSettings
from .policy import AgentSettings, AssistantSettings, SystemSettings
from .storage import Database, dumps, loads

log = logging.getLogger(__name__)

SCOPES = ("global", "group", "item")
GLOBAL_KEY = "*"


class SettingsError(ValueError):
    pass


class PermissionDenied(PermissionError):
    pass


class SettingsStore:
    def __init__(
        self,
        db: Database,
        base_system: Optional[dict] = None,
        clock: Clock = utcnow,
        base_assistant: Optional[dict] = None,
        base_agents: Optional[dict] = None,
        base_packs: Optional[dict] = None,
    ) -> None:
        self.db = db
        self.clock = clock
        self._base_system = base_system or {}
        self._system_model: type = SystemSettings
        # Module settings (one flat model per module, admin-editable, audited). The core's own are here; a pack adds its own with register_section().
        self._sections: dict[str, tuple[type, dict]] = {
            "assistant": (AssistantSettings, base_assistant or {}),
            "agents": (AgentSettings, base_agents or {}),
            "packs": (PackSettings, base_packs or {}),
        }
        # Settings that were removed. Saved values for them are ignored (and dropped on the next save of that module).
        self._retired: dict[str, tuple[str, ...]] = {}
        self._scoped: Optional[tuple[str, type, dict]] = None
        # Set when a pack that adds system fields is switched off: its values stay in the settings file and the database, but are ignored (not an error) until it is on again.
        self.ignore_unknown_system = False
        self._listeners: list[Callable[[str, str], None]] = []
        for model, base in self._sections.values():                # the core's own sections are checked at once; the system settings wait for the packs (validate())
            model.model_validate(base)

    def _system_values(self, *layers: dict) -> dict:
        merged: dict[str, Any] = {}
        for layer in layers:
            merged.update(layer)
        if self.ignore_unknown_system:
            known = self._system_model.model_fields
            merged = {k: v for k, v in merged.items() if k in known}
        return merged

    def validate(self) -> None:
        """Fail fast if the values in the settings file are invalid. Called once every pack has registered, because a pack may add fields to the system settings."""
        self._system_model.model_validate(self._system_values(self._base_system))
        for model, base in self._sections.values():
            model.model_validate(base)
        if self._scoped is not None:
            self._scoped[1].model_validate(self._scoped[2])

    # ---------------------------------------------------------------- what a pack registers
    def register_section(self, name: str, model: type, base: Optional[dict] = None, retired: tuple[str, ...] = ()) -> None:
        """Add a module's settings section. Registering the same section twice with the same model is harmless; a different model under a taken name is an error."""
        base = base or {}
        if name in self._sections:
            if self._sections[name][0] is not model:
                raise SettingsError(f"the settings section {name!r} is already registered by something else")
            return
        model.model_validate(base)
        self._sections[name] = (model, base)
        if retired:
            self._retired[name] = tuple(retired)

    def extend_system(self, model: type) -> None:
        """A pack adds fields to the system settings by passing a subclass of SystemSettings. Several packs combine; the values from the settings file are checked by validate()."""
        if not issubclass(model, SystemSettings):
            raise SettingsError("extend_system needs a subclass of SystemSettings")
        if issubclass(self._system_model, model):
            return
        if issubclass(model, self._system_model):
            combined = model
        else:
            combined = type("SystemSettings", (model, self._system_model), {})
        self._system_model = combined

    def register_scoped(self, kind: str, model: type, base: Optional[dict] = None) -> None:
        """Add settings that can be overridden per item group and per item (the stock thresholds). One such kind is supported: its saved rows keep the plain scope names
        (global, group, item), which is how databases made before packs existed already store them."""
        base = base or {}
        if self._scoped is not None:
            if self._scoped[0] == kind and self._scoped[1] is model:
                return
            raise SettingsError("only one settings kind with per-group and per-item overrides is supported")
        model.model_validate(base)
        self._scoped = (kind, model, base)

    def _scope(self, kind: str) -> tuple[type, dict]:
        if self._scoped is None or self._scoped[0] != kind:
            raise SettingsError(f"unknown scoped settings {kind!r} (is the pack that adds it switched on?)")
        return self._scoped[1], self._scoped[2]

    def schemas(self) -> dict[str, Any]:
        """Field metadata for admin UIs, for everything registered (name, type, default, description, constraints)."""
        out: dict[str, Any] = {}
        if self._scoped is not None:
            out[self._scoped[0]] = self._scoped[1].model_json_schema()["properties"]
        out["system"] = self._system_model.model_json_schema()["properties"]
        for name, (model, _base) in self._sections.items():
            out[name] = model.model_json_schema()["properties"]
        return out

    # ---------------------------------------------------------------- listeners
    def on_change(self, fn: Callable[[str, str], None]) -> None:
        """fn(scope_type, scope_key) is called after every successful write (e.g. to reschedule)."""
        self._listeners.append(fn)

    # ---------------------------------------------------------------- reads
    def get_overrides(self, scope_type: str, scope_key: str) -> dict[str, Any]:
        row = self.db.query_one(
            "SELECT data_json FROM settings_overrides WHERE scope_type=? AND scope_key=?", (scope_type, scope_key)
        )
        return loads(row["data_json"]) if row else {}

    def effective_scoped(self, kind: str, item_code: Optional[str] = None, item_group: Optional[str] = None) -> Any:
        return self._resolve(kind, item_code, item_group)[0]

    def scoped_with_sources(self, kind: str, item_code: Optional[str] = None, item_group: Optional[str] = None) -> tuple[Any, dict[str, str]]:
        """Effective settings plus, for every field an admin changed, where it came from."""
        return self._resolve(kind, item_code, item_group)

    def _resolve(self, kind: str, item_code: Optional[str], item_group: Optional[str]) -> tuple[Any, dict[str, str]]:
        model, base = self._scope(kind)
        merged: dict[str, Any] = dict(base)
        sources: dict[str, str] = {k: "config file" for k in base}
        layers = [("global", GLOBAL_KEY)]
        if item_group:
            layers.append(("group", item_group))
        if item_code:
            layers.append(("item", item_code))
        for scope_type, key in layers:
            ov = self.get_overrides(scope_type, key)
            for field, value in ov.items():
                merged[field] = value
                sources[field] = f"{scope_type}{'' if scope_type == 'global' else ' ' + repr(key)} override"
        try:
            return model.model_validate(merged), sources
        except ValidationError as exc:
            raise SettingsError(f"Invalid effective policy for item={item_code!r} group={item_group!r}: {_first_error(exc)}") from exc

    def system(self) -> Any:
        return self._system_model.model_validate(self._system_values(self._base_system, self.get_overrides("system", GLOBAL_KEY)))

    def _section_overrides(self, kind: str) -> dict[str, Any]:
        """Saved values for a module, without settings that no longer exist (so an old save can never break loading)."""
        _model, _base = self._sections[kind]
        return {k: v for k, v in self.get_overrides(kind, GLOBAL_KEY).items() if k not in self._retired.get(kind, ())}

    def section(self, kind: str) -> Any:
        if kind not in self._sections:
            raise SettingsError(f"unknown settings section {kind!r}")
        model, base = self._sections[kind]
        saved = self._section_overrides(kind)
        try:
            return model.model_validate({**base, **saved})
        except ValidationError as exc:
            # A value saved under an older, looser rule (for example a message template the strict check now refuses) must not stop the whole module from loading:
            # that one value is ignored, the default is used, and the log says which.
            bad = {str(e["loc"][0]) for e in exc.errors() if e.get("loc") and str(e["loc"][0]) in saved}
            if not bad:
                raise
            log.warning("the saved value of %s.%s is no longer valid (%s) and is ignored: the default is used until it is saved again", kind, ", ".join(sorted(bad)), _first_error(exc))
            return model.model_validate({**base, **{k: v for k, v in saved.items() if k not in bad}})

    def agents(self) -> AgentSettings:
        return self.section("agents")

    def assistant(self) -> AssistantSettings:
        return self.section("assistant")

    def packs(self) -> PackSettings:
        return self.section("packs")

    # ---------------------------------------------------------------- writes (admin only)
    def update_scoped(
        self,
        actor: Actor,
        kind: str,
        scope_type: str,
        scope_key: str,
        set_values: Optional[dict[str, Any]] = None,
        clear: Optional[list[str]] = None,
    ) -> Any:
        """`set_values` may contain None for nullable thresholds (e.g. min_qty=None switches the rule off).
        `clear` removes an override so the value is inherited again."""
        self._require_admin(actor)
        model, base = self._scope(kind)
        if scope_type not in SCOPES:
            raise SettingsError(f"scope_type must be one of {SCOPES}")
        if scope_type == "global":
            scope_key = GLOBAL_KEY
        elif not scope_key or scope_key == GLOBAL_KEY:
            raise SettingsError("scope_key is required for group/item scope")
        set_values = set_values or {}
        clear = clear or []
        unknown = [f for f in [*set_values, *clear] if f not in model.model_fields]
        if unknown:
            raise SettingsError(f"Unknown policy field(s): {', '.join(unknown)}")

        old = self.get_overrides(scope_type, scope_key)
        new = {k: v for k, v in old.items() if k not in clear}
        new.update(set_values)

        # Validate: the new override layered on config + global (+ nothing else known here).
        probe = {**base}
        if scope_type != "global":
            probe.update(self.get_overrides("global", GLOBAL_KEY))
        probe.update(new)
        try:
            validated = model.model_validate(probe)
        except ValidationError as exc:
            raise SettingsError(_first_error(exc)) from exc

        self._save(kind, scope_type, scope_key, old, new, actor)
        return validated

    def reset_scoped(self, actor: Actor, kind: str, scope_type: str, scope_key: str) -> None:
        self._require_admin(actor)
        self._scope(kind)
        if scope_type == "global":
            scope_key = GLOBAL_KEY
        old = self.get_overrides(scope_type, scope_key)
        if old:
            self._save(kind, scope_type, scope_key, old, {}, actor)

    def update_system(self, actor: Actor, set_values: dict[str, Any]) -> Any:
        self._require_admin(actor)
        unknown = [f for f in set_values if f not in self._system_model.model_fields]
        if unknown:
            raise SettingsError(f"Unknown system field(s): {', '.join(unknown)}")
        old = self.get_overrides("system", GLOBAL_KEY)
        new = {**old, **set_values}
        try:
            validated = self._system_model.model_validate(self._system_values(self._base_system, new))
        except ValidationError as exc:
            raise SettingsError(_first_error(exc)) from exc
        self._save("system", "system", GLOBAL_KEY, old, new, actor)
        return validated

    def update_section(self, actor: Actor, kind: str, set_values: dict[str, Any], clear: Optional[list[str]] = None) -> Any:
        """Change a module's settings. `clear` removes an override so the config-file/default value applies again."""
        self._require_admin(actor)
        if kind not in self._sections:
            raise SettingsError(f"unknown settings section {kind!r}")
        model, base = self._sections[kind]
        clear = clear or []
        unknown = [f for f in [*set_values, *clear] if f not in model.model_fields]
        if unknown:
            raise SettingsError(f"Unknown {kind} field(s): {', '.join(unknown)}")
        old = self._section_overrides(kind)
        new = {k: v for k, v in old.items() if k not in clear}
        new.update(set_values)
        try:
            validated = model.model_validate({**base, **new})
        except ValidationError as exc:
            raise SettingsError(_first_error(exc)) from exc
        self._save(kind, kind, GLOBAL_KEY, old, new, actor)
        return validated

    # ---------------------------------------------------------------- audit
    def audit(self, limit: int = 100, scope_type: Optional[str] = None, scope_key: Optional[str] = None) -> list[dict]:
        sql = "SELECT * FROM settings_audit"
        params: list[Any] = []
        conds = []
        if scope_type:
            conds.append("scope_type=?")
            params.append(scope_type)
        if scope_key:
            conds.append("scope_key=?")
            params.append(scope_key)
        if conds:
            sql += " WHERE " + " AND ".join(conds)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        return [
            {
                "id": r["id"], "ts": r["ts"], "actor": r["actor"], "scope_type": r["scope_type"],
                "scope_key": r["scope_key"], "field": r["field"],
                "old": loads(r["old_json"]), "new": loads(r["new_json"]),
            }
            for r in self.db.query(sql, params)
        ]

    # ---------------------------------------------------------------- internals
    def _require_admin(self, actor: Actor) -> None:
        if not actor.has_role("admin"):
            raise PermissionDenied(f"{actor.id!r} is not allowed to change settings (admin role required)")

    def _save(self, kind: str, scope_type: str, scope_key: str, old: dict, new: dict, actor: Actor) -> None:
        now = iso(self.clock())
        db_scope = kind if kind == "system" or kind in self._sections else scope_type
        with self.db.transaction():
            if new:
                self.db.execute(
                    "INSERT INTO settings_overrides(scope_type, scope_key, data_json, updated_by, updated_at) VALUES (?,?,?,?,?) "
                    "ON CONFLICT(scope_type, scope_key) DO UPDATE SET data_json=excluded.data_json, "
                    "updated_by=excluded.updated_by, updated_at=excluded.updated_at",
                    (db_scope, scope_key, dumps(new), actor.id, now),
                )
            else:
                self.db.execute("DELETE FROM settings_overrides WHERE scope_type=? AND scope_key=?", (db_scope, scope_key))
            for field in sorted(set(old) | set(new)):
                before, after = old.get(field, "<inherited>"), new.get(field, "<inherited>")
                if before != after:
                    self.db.execute(
                        "INSERT INTO settings_audit(ts, actor, scope_type, scope_key, field, old_json, new_json) VALUES (?,?,?,?,?,?,?)",
                        (now, actor.id, db_scope, scope_key, field, dumps(before), dumps(after)),
                    )
        for fn in self._listeners:
            fn(db_scope, scope_key)


def _first_error(exc: ValidationError) -> str:
    err = exc.errors()[0]
    loc = ".".join(str(x) for x in err.get("loc", ()))
    msg = err.get("msg", "invalid value")
    return f"{loc}: {msg}" if loc else msg
