"""Command line: try it, operate it, and administer it without touching the API."""
from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import sys
from functools import lru_cache
from pathlib import Path
from typing import Any, Optional

from . import obs
from .approvals.channels.telegram import TelegramChannel
from .approvals.render import render_plain
from .bootstrap import App, StartupRefused, build_app
from .config import load_config
from . import doctor as doctor_checks
from . import ops
from .common import Actor
from .packs import PackRegistry

log = logging.getLogger(__name__)

CLI_ACTOR = Actor(id="cli", roles=frozenset({"admin", "approver"}), via="cli")


def _app(args) -> App:
    return build_app(config=load_config(args.config))


def pack_app(args, name: str) -> Optional[App]:
    """For a command a pack adds: build the application, or say plainly (and return None) when that pack is switched off."""
    app = _app(args)
    if not app.packs.is_enabled(name) or name not in app.packs.loaded:
        print(f"This command belongs to the {name} pack, which is switched off (settings: packs.enabled.{name}).")
        return None
    return app


@lru_cache(maxsize=1)
def installed_packs() -> tuple[Any, ...]:
    """The code of every installed pack that has any, loaded so that its commands can be offered whatever the settings say; a command of a pack that is switched off explains that."""
    registry = PackRegistry.discover()
    out = []
    for name, manifest in registry.manifests.items():
        if manifest.status == "skeleton":
            continue
        try:
            out.append(registry.load(name))
        except Exception as exc:  # noqa: BLE001 - one broken pack must not take the whole command line down
            log.warning("pack %s could not be loaded for the command line: %s", name, exc)
    return tuple(out)


# ------------------------------------------------------------------ commands
def cmd_init(args) -> int:
    target = Path(args.dir) / "config"
    target.mkdir(parents=True, exist_ok=True)
    src = Path(__file__).resolve().parent.parent / "settings.example.yaml"
    dst = target / "settings.yaml"
    if dst.exists() and not args.force:
        print(f"{dst} already exists (use --force to overwrite)")
        return 1
    shutil.copy(src, dst)
    print(f"Wrote {dst}. Edit it, set the environment variables it mentions, then run: erp-agents run-once --dry-run")
    return 0


def cmd_approvals(args) -> int:
    app = _app(args)
    if args.action == "list":
        for rec in app.approvals.list(args.status or None):
            print(f"{rec.id}  {rec.status:<9} {rec.request.severity:<13} {rec.request.headline}")
        return 0
    res = app.approvals.decide(args.id, args.choice, CLI_ACTOR, "cli")
    if res.status == "info":
        print(render_plain(res.approval.request, sections=res.info_sections, with_title=False))
    else:
        print(res.message)
    return 0 if res.ok else 1


def cmd_history(args) -> int:
    """"Why did you do that?": search the decision history, show one decision in full, export it, or check its seal."""
    app = _app(args)
    if args.verify:
        app.audit.sweep()
        res = app.audit.verify()
        print(f"{'OK' if res['ok'] else 'PROBLEMS FOUND'}: {res['entries']} sealed entries, latest {res['latest_at'] or '-'}")
        print(f"latest hash: {res['latest_hash']}  (keep a copy somewhere else: a later check can then prove nothing before it was rewritten)")
        for p in res["problems"]:
            print(f"  - {p}")
        return 0 if res["ok"] else 1
    if args.show:
        e = app.history.entry(args.show)
        if e is None:
            print(f"No request {args.show}.")
            return 1
        print(json.dumps(e, indent=2, default=str))
        return 0
    filters = {"q": " ".join(args.words or []), "kind": args.kind, "item": args.item, "who": args.who, "since": args.since, "until": args.until, "status": args.status}
    try:
        if args.csv:
            Path(args.csv).write_text(app.history.csv(**filters), encoding="utf-8", newline="")
            print(f"Written to {args.csv}")
            return 0
        res = app.history.search(limit=args.limit, **filters)
    except ValueError as exc:
        print(exc)
        return 2
    print(f"{res['total']} decision{'s' if res['total'] != 1 else ''}" + (f", newest {res['listed']} shown" if res["listed"] < res["total"] else ""))
    for e in res["entries"]:
        print(f"\n{e['at'][:16].replace('T', ' ')}  {e['id']}  {e['title']}")
        print(f"   {e['outcome']}")
        for line in e["why"][:3]:
            print(f"   why: {line}")
    return 0


def cmd_erp_check(args) -> int:
    app = _app(args)
    print("healthcheck:", "OK" if app.erp.healthcheck() else "FAILED")
    items = app.erp.list_items()
    print(f"{len(items)} stock items found")
    for it in items[:5]:
        st = app.erp.get_stock([it.code])[it.code]
        print(f"  {it.code}: {it.name} | group={it.group} | on hand {st.on_hand:g} | on order {st.incoming:g} | lead time {it.lead_time_days}")
    print("Compare these quantities with Stock > Stock Balance in ERPNext before trusting any alert.")
    return 0


def cmd_erp_setup(args) -> int:
    """One-time ERPNext customisation: the 'Agent proposal reference' and approver fields on Purchase Order, Material Request and Stock Entry (prevents duplicates, records the approver)."""
    app = _app(args)
    ensure = getattr(app.erp, "ensure_po_reference_field", None)
    if ensure is None:
        print("this ERP adapter needs no setup")
        return 0
    try:
        print(f"Fields 'Agent proposal reference' and 'Approved by (Stock Assistant)' on Purchase Order, Material Request and Stock Entry: {ensure()}")
    except Exception as exc:  # noqa: BLE001
        print(f"setup failed: {exc}")
        return 1
    return 0


def cmd_erp_check_access(args) -> int:
    """Read-only: is the service account limited to what the Stock Assistant needs?"""
    from .auth.access import check_service_account
    app = _app(args)
    erp = app.erp
    if not hasattr(erp, "session"):
        print("this ERP adapter has no service account to check")
        return 0
    needs = app.packs.erp_permissions()
    if not needs:
        print("No pack that is switched on declares anything it needs from the ERPNext service account, so there is nothing to check it against.")
        return 0
    r = check_service_account(erp.session, erp.base_url, needs)
    print(f"Service account: {r.user}, {len(r.roles)} roles")
    if r.missing:
        print("It is MISSING access the app needs:", ", ".join(f"{d} ({p})" for d, p in r.missing))
    print(f"Roles that cover everything the app needs ({len(r.keep)}): {', '.join(r.keep) or 'none'}")
    if r.surplus:
        print(f"Roles it does not need ({len(r.surplus)}): {', '.join(r.surplus)}")
    if r.risky_held:
        print("High-risk roles among them:", ", ".join(r.risky_held))
        print("Remove these in ERPNext (User > Roles) from the service account; a stolen key could otherwise change anything in ERPNext.")
    elif not r.surplus:
        print("Nothing to remove.")
    return 1 if r.missing else 0


def cmd_telegram(args) -> int:
    app = _app(args)
    ch = app.channels.get("telegram")
    if not isinstance(ch, TelegramChannel):
        print("The Telegram channel is not switched on. Put TELEGRAM_BOT_TOKEN in .env (create the bot with @BotFather), then try again.")
        return 1
    try:
        return _telegram_action(app, ch, args)
    except (RuntimeError, ValueError) as exc:
        print(f"Telegram said no: {exc}")
        return 1


def _telegram_recipients(app) -> list[tuple[str, str]]:
    return [(r.id, c.address or "") for r in app.router.recipients for c in r.channels if c.type == "telegram"]


def _telegram_action(app, ch: TelegramChannel, args) -> int:
    if args.action == "set-webhook":
        if not args.url:
            print("Give the public address: erp-agents telegram set-webhook https://<your-tunnel>/webhooks/telegram")
            return 1
        print(ch.set_webhook(args.url))
        return 0
    if args.action == "delete-webhook":
        print(ch.delete_webhook())
        return 0
    if args.action == "chats":
        chats = ch.recent_chats()
        if not chats:
            print("Nobody has written to the bot in the last 24 hours. Open your bot in Telegram, press Start (or send /start), then run this again.\n"
                  "(If the app is running in polling mode it answers /start itself with the chat id, and this list stays empty because the app already read the message.)")
            return 0
        print("People who wrote to the bot (use the chat id as OWNER_TELEGRAM_CHAT_ID):")
        for c in chats:
            print(f"  chat id {c['chat_id']:>12}   {c['name']}" + (f" (@{c['username']})" if c["username"] else "") + f"   last wrote: {c['last_text']!r}")
        return 0
    if args.action == "test":
        targets = [(rid, addr) for rid, addr in _telegram_recipients(app) if ch.address_problem(addr) is None]
        if not targets:
            print("No recipient has a usable Telegram chat id yet. Write /start to the bot, put the number it sends back in OWNER_TELEGRAM_CHAT_ID, restart.")
            return 1
        bad = 0
        for rid, addr in targets:
            res = ch.send_text(addr, "Stock Assistant: this is a test message. If you can read it, Telegram is connected and low-stock alerts will arrive here.")
            print(f"  {rid} (chat {addr}): " + ("delivered" if res.ok else f"FAILED - {res.error}"))
            bad += 0 if res.ok else 1
        return 1 if bad else 0
    # status
    me = ch.me()
    print(f"Bot: @{me.get('username')} ({me.get('first_name')})   mode: {ch.mode}")
    info = ch.webhook_info()
    if ch.mode == "polling":
        print("Webhook: " + ("NONE (good for polling)" if not info.get("url") else f"still registered ({info['url']}); the app clears it when it starts in polling mode"))
    else:
        print("Webhook: " + (info.get("url") or "NOT registered: run telegram set-webhook https://<your-tunnel>/webhooks/telegram"))
        if info.get("last_error_message"):
            print(f"  Telegram's last delivery error: {info['last_error_message']}")
        print(f"  waiting updates: {info.get('pending_update_count', 0)}")
    recipients = _telegram_recipients(app)
    if not recipients:
        print("Recipients on Telegram: none listed in approvals.recipients")
    for rid, addr in recipients:
        problem = ch.address_problem(addr)
        print(f"Recipient {rid}: " + ("chat id " + addr + " ok" if problem is None else "NOT READY - the address " + problem))
    return 0


def cmd_mcp(args) -> int:
    """Serve the stock tools to an MCP client (Claude Desktop, another agent) over stdin/stdout. Who the client is: `mcp.stdio_role` in the settings file (viewer or approver)."""
    try:
        from .mcp_server import build_mcp_server
    except ImportError:
        print('The MCP server needs an extra package: pip install "erpnext-ai-agents[agents]"', file=sys.stderr)
        return 2
    import anyio
    app = _app(args)
    role = str((app.config.get("mcp") or {}).get("stdio_role", "viewer")).lower()
    if role not in ("viewer", "approver"):
        print("mcp.stdio_role must be 'viewer' (look only) or 'approver' (may also prepare an order request for a person to answer).", file=sys.stderr)
        return 2
    console = app.channels.get("console")
    if console is not None:                           # stdout belongs to the protocol: nothing else may be written to it
        console.writer = lambda text: print(text, file=sys.stderr)
    actor = Actor(id="mcp:stdio", roles=frozenset({role}), via="mcp-stdio")
    print(f"Stock Assistant MCP server (stdio) is ready, acting as '{role}'.", file=sys.stderr)
    anyio.run(build_mcp_server(app.toolkit, default_actor=actor, via="mcp-stdio").run_stdio_async)
    return 0


def _tokens_text(tokens: dict[str, Any]) -> str:
    text = f"{tokens['total']:,} tokens ({tokens['input']:,} in, {tokens['output']:,} out)"
    return text + (f", about {tokens['estimated_cost']:g} at your prices" if tokens.get("estimated_cost") is not None else "")


def print_agent_result(res: dict[str, Any]) -> None:
    who = f"model {res['model']} via {res.get('provider', 'ollama')}, {res.get('seconds', 0)}s" if res.get("source") == "model" else "plain rules (no AI used)"
    if res.get("hosted") and res.get("tokens"):
        who += "; " + _tokens_text(res["tokens"])
    if res.get("text"):
        print(res["text"])
    elif res.get("answer") is not None:
        print(res["answer"])
    print(f"\n[{who}; task {res.get('task_id') or '-'}]" + (f" Note: {res['note']}" if res.get("note") else ""))


GENERIC_AGENT_ACTIONS = ("status", "privacy", "tasks", "usage")


def cmd_agents(args) -> int:
    from .agents.privacy import privacy_summary
    app = _app(args)
    svc = app.agents
    pack_actions = {name: handler for pack in app.packs.loaded.values() for name, handler in pack.agent_actions().items()}
    if args.action == "status":
        print(json.dumps(svc.status(check_model=True), indent=2, default=str))
    elif args.action == "privacy":
        print(json.dumps(privacy_summary(svc.settings, app.packs.privacy_lines()), indent=2))
    elif args.action in pack_actions:                  # brief, review, ask ...: what a pack that is switched on adds
        return pack_actions[args.action](args, app)
    elif args.action not in GENERIC_AGENT_ACTIONS:
        print(f"'agents {args.action}' belongs to a pack that is switched off (settings: packs.enabled).")
        return 1
    elif args.action == "usage":
        u = svc.usage(args.limit)
        month = u["month_hosted"]
        print(f"This month (since {u['month_since']}), hosted models: {month['tasks']} tasks, {_tokens_text(month)}"
              + (f"; limit {u['monthly_token_limit']:,} tokens, {u['limit_used_percent']}% used" if u["monthly_token_limit"] else "; no monthly limit"))
        print(f"This month, models on this computer: {u['month_local']['tasks']} tasks, {u['month_local']['total']:,} tokens (free)")
        print(f"Last {u['days']} days by model:")
        for r in u["by_model"]:
            cost = f", about {r['estimated_cost']:g}" if r["estimated_cost"] is not None else ""
            print(f"  {r['provider']:9} {r['model']:28} {'hosted' if r['hosted'] else 'local ':6} {r['tasks']:>4} tasks  {r['input']:>10,} in  {r['output']:>9,} out{cost}")
    elif args.action == "tasks":
        for t in svc.log.list(limit=args.limit):
            print(f"{t['started_at'][:19]}  {t['id']}  {t['agent']}/{t['task']}  {t['status']:9}  {t['llm_seconds'] or 0:>6}s  steps={t['steps']} tools={t['tool_calls']}  {t['model']}  {t['note'] or ''}")
    return 0


def _db_path(args) -> str:
    return str((load_config(args.config).get("database") or {}).get("path", "./data/erp_agents.db"))


def cmd_doctor(args) -> int:
    """A read-only check of this installation: files, permissions, settings, database, backups. No network, no change."""
    cfg = load_config(args.config)
    path = args.config or os.environ.get("ERP_AGENTS_CONFIG", "config/settings.yaml")
    checks = doctor_checks.run_doctor(cfg, path)
    print(doctor_checks.render(checks))
    return doctor_checks.exit_code(checks, strict=args.strict)


def cmd_backup(args) -> int:
    db = _db_path(args)
    try:
        made = ops.backup_database(db, args.to)
    except ops.OpsError as exc:
        print(f"Backup refused: {exc}")
        return 1
    print(f"Backup made and verified: {made}\n"
          "It holds the database only. Copy .env and the settings file separately (they are not in it), and keep the copies somewhere other than this computer.")
    return 0


def cmd_backup_verify(args) -> int:
    verdict = ops.verify_database(args.file)
    print(f"{args.file}: {verdict.explain()}")
    return 0 if verdict.ok else 1


def cmd_restore(args) -> int:
    db = _db_path(args)
    try:
        kept = ops.restore_database(args.file, db, force=args.force)
    except ops.OpsError as exc:
        print(f"Restore refused, nothing was changed: {exc}")
        return 1
    print(f"Restored {args.file} to {db}." + (f"\nThe database it replaced is kept as {kept} (delete it yourself when you are sure)." if kept else ""))
    return 0


def cmd_serve(args) -> int:
    from contextlib import asynccontextmanager

    import uvicorn
    from .api.app import create_app
    from .scheduler import AgentScheduler

    db_path = _db_path(args)
    lock = ops.InstanceLock(db_path) if db_path != ":memory:" else None
    try:
        if lock is not None:
            lock.acquire()                      # one running instance per database: before the file is even opened
    except ops.AlreadyRunning as exc:
        print(f"Not started: {exc}")
        return 1
    try:
        return _serve(args, create_app, uvicorn, asynccontextmanager, AgentScheduler)
    finally:
        if lock is not None:
            lock.release()


def _serve(args, create_app, uvicorn, asynccontextmanager, AgentScheduler) -> int:
    app = _app(args)
    fmt = obs.log_format_from(args.log_format, app.observability.log_format)         # the flag and the environment win over the settings file
    if fmt != obs.log_format_from(args.log_format):
        obs.configure_logging(logging.DEBUG if args.verbose_log else logging.INFO, fmt)
    scheduler = AgentScheduler(app)

    @asynccontextmanager
    async def lifespan(_):
        scheduler.start()
        try:
            yield
        finally:
            scheduler.shutdown()

    api = create_app(app, on_system_change=scheduler.reschedule, lifespan=lifespan)
    srv = app.config.get("server", {})
    host = args.host or srv.get("host", "127.0.0.1")
    mode = str(srv.get("remote_access", "full")).lower()
    print(f"Remote access: {'only the channel webhooks are reachable through a tunnel or proxy' if mode == 'webhooks_only' else 'the console and API are reachable through a tunnel or proxy (server.remote_access: webhooks_only closes them)'}.")
    from .api.hardening import access_key_warning
    warning = access_key_warning(app.config.get("auth"), bool(app.tokens))
    if warning:
        print(warning)
    if host not in ("127.0.0.1", "::1", "localhost"):
        print(f"WARNING: listening on {host}, not only on this machine. Anybody who can reach that address can reach the sign-in page.")
    # in the JSON format uvicorn's own lines (start-up, access) go through our handler too, so every line is one JSON object; in the text format it keeps its usual look
    uvicorn.run(api, host=host, port=args.port or int(srv.get("port", 8100)), log_level="info", server_header=False, **({"log_config": None} if fmt == "json" else {}))
    return 0


# ------------------------------------------------------------------ parser
def build_parser() -> argparse.ArgumentParser:
    packs = installed_packs()
    p = argparse.ArgumentParser(prog="erp-agents", description="Agentic inventory monitoring for small-business ERP")
    p.add_argument("--config", default=None, help="path to settings.yaml (default: $ERP_AGENTS_CONFIG or config/settings.yaml)")
    p.add_argument("-v", "--verbose-log", action="store_true", help="debug logging")
    p.add_argument("--log-format", choices=["text", "json"], default=None,
                   help="how log lines are written: text (the default) or json, one object per line (also: $ERP_AGENTS_LOG_FORMAT, or observability.log_format in the settings file)")
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("init", help="write a starter config/settings.yaml")
    s.add_argument("--dir", default=".")
    s.add_argument("--force", action="store_true")
    s.set_defaults(fn=cmd_init)

    for pack in packs:                                          # the commands each installed pack adds (demo, run-once, analyze, policy ... for the stock pack)
        for command in pack.cli_commands():
            s = sub.add_parser(command.name, help=command.help)
            command.configure(s)
            s.set_defaults(fn=command.run)

    s = sub.add_parser("approvals", help="list/answer approval requests")
    s2 = s.add_subparsers(dest="action", required=True)
    ls = s2.add_parser("list")
    ls.add_argument("--status", default="pending")
    dc = s2.add_parser("decide")
    dc.add_argument("id")
    dc.add_argument("choice", help="approve | snooze | dismiss | details")
    s.set_defaults(fn=cmd_approvals)

    s = sub.add_parser("history", help="why did you do that? search every past decision with its reasoning, show one, export it, check its seal")
    s.add_argument("words", nargs="*", help="words that must all appear (item codes, vendor names, order numbers ...)")
    s.add_argument("--item", help="only this item code")
    s.add_argument("--kind", help="only this kind of request, e.g. procurement.po")
    s.add_argument("--who", help="only decisions by this person")
    s.add_argument("--since", help="from this day (YYYY-MM-DD)")
    s.add_argument("--until", help="up to and including this day (YYYY-MM-DD)")
    s.add_argument("--status", choices=["pending", "decided", "expired", "cancelled", "changed"])
    s.add_argument("--limit", type=int, default=20)
    s.add_argument("--show", metavar="ID", help="one request in full")
    s.add_argument("--csv", metavar="FILE", help="write the result to a CSV file")
    s.add_argument("--verify", action="store_true", help="check that nothing was changed or deleted after it was sealed")
    s.set_defaults(fn=cmd_history, leftovers="words")
    s = sub.add_parser("erp-check", help="test the ERP connection and print sample stock")
    s.set_defaults(fn=cmd_erp_check)

    s = sub.add_parser("erp-setup", help="one-time: add the 'Agent proposal reference' and approver fields to Purchase Order, Material Request and Stock Entry in ERPNext")
    s.set_defaults(fn=cmd_erp_setup)

    s = sub.add_parser("erp-check-access", help="read-only: does the ERPNext service account hold only the roles the app needs?")
    s.set_defaults(fn=cmd_erp_check_access)

    s = sub.add_parser("telegram", help="Telegram: status, test message, find chat ids, register/remove the webhook")
    s.add_argument("action", choices=["status", "test", "chats", "set-webhook", "delete-webhook"])
    s.add_argument("url", nargs="?", help="set-webhook only: the public https address of /webhooks/telegram")
    s.set_defaults(fn=cmd_telegram)

    s = sub.add_parser("mcp", help="serve the stock tools to an MCP client over stdio (needs the 'agents' extra)")
    s.set_defaults(fn=cmd_mcp)

    s = sub.add_parser("agents", help="the AI agents: status, today's stock brief, a second opinion on a request, ask a question, what was sent to the model")
    extra = [name for pack in packs for name in pack.agent_actions() if name not in GENERIC_AGENT_ACTIONS]
    s.add_argument("action", choices=[*GENERIC_AGENT_ACTIONS, *extra])
    s.add_argument("text", nargs="*", help="review: the request id; ask: the question")
    s.add_argument("--rules", action="store_true", help="brief/review: use rules only, send nothing to any model")
    s.add_argument("--force", action="store_true", help="review: ask again even if this request was reviewed")
    s.add_argument("--limit", type=int, default=15, help="tasks: how many; usage: how many days back")
    s.set_defaults(fn=cmd_agents)

    s = sub.add_parser("doctor", help="read-only check of this installation: file permissions, exposure, settings, database, backups (no network)")
    s.add_argument("--strict", action="store_true", help="exit with an error for warnings too, not only for things to fix now")
    s.set_defaults(fn=cmd_doctor)

    s = sub.add_parser("backup", help="a consistent, verified copy of the database (safe while the application runs)")
    s.add_argument("--to", help="file to write (default: data/backups/<name>-<time>.db); an existing file is never overwritten")
    s.set_defaults(fn=cmd_backup)

    s = sub.add_parser("backup-verify", help="is this file a complete, undamaged database that this version understands?")
    s.add_argument("file")
    s.set_defaults(fn=cmd_backup_verify)

    s = sub.add_parser("restore", help="put a verified backup in place (stop the application first; the database it replaces is kept beside it)")
    s.add_argument("file")
    s.add_argument("--force", action="store_true", help="replace the database that is there")
    s.set_defaults(fn=cmd_restore)

    s = sub.add_parser("serve", help="start the API, scheduler and channel listeners")
    s.add_argument("--host")
    s.add_argument("--port", type=int)
    s.set_defaults(fn=cmd_serve)
    return p


def parse_cli(parser: argparse.ArgumentParser, argv: Optional[list[str]] = None) -> argparse.Namespace:
    """parse_args, except that `policy set --scope item --key X min_qty=5` also works on Python 3.10 and 3.11. There argparse matches the optional positional (the
    field=value list) before it has seen the options, then rejects what follows them as unrecognised. Anything left over belongs to that list."""
    args, extra = parser.parse_known_args(argv)
    if extra:
        target = getattr(args, "leftovers", None)           # a command whose last argument is a list (policy set ... field=value) says where leftovers belong
        if target and not any(x.startswith("-") for x in extra):
            setattr(args, target, [*getattr(args, target), *extra])
        else:
            parser.error("unrecognized arguments: " + " ".join(extra))
    return args


def main(argv: Optional[list[str]] = None) -> int:
    for stream in (sys.stdout, sys.stderr):      # old Windows code pages must not crash on the emoji/bullets in messages
        try:
            stream.reconfigure(errors="replace")
        except Exception:  # noqa: BLE001  # not a real text stream (e.g. under test capture): nothing to reconfigure  # nosec B110
            pass
    args = parse_cli(build_parser(), argv)
    level = logging.DEBUG if args.verbose_log else logging.INFO
    try:
        fmt = obs.log_format_from(args.log_format)
    except ValueError as exc:
        print(f"erp-agents: {exc}", file=sys.stderr)
        return 2
    if args.log_format or os.environ.get(obs.LOG_FORMAT_ENV) or not logging.getLogger().handlers:        # as basicConfig did: leave a logging set-up that is already there alone
        obs.configure_logging(level, fmt)
    try:
        return args.fn(args)
    except StartupRefused as exc:                # a configuration it will not start with: say why, in words, without a traceback
        print(f"erp-agents: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
