# Watching it run: logs, readiness, metrics, traces

This page is for whoever looks after the Stock Assistant: how to see what it is doing, how to know it is stuck, and what to do when it is. Nothing here changes what the assistant decides or writes; it only lets you see it. Everything beyond the plain log is **off until you switch it on**.

| You want to know | Use | On by default |
|---|---|---|
| Is the process answering? | `GET /health` | yes (unchanged) |
| Can it do its work (database writable, scheduler alive)? | `GET /ready` | yes |
| What happened to this one task? | the log, searched by its task id | yes |
| How is it doing over days (errors, slow ERPNext, fallbacks)? | `GET /metrics` into Prometheus | **no** |
| Where did the time of one task go? | traces (OpenTelemetry) | **no** |
| Is the installation itself in good shape? | `erp-agents doctor` | run it when you like |

## Settings

All in the `observability:` section of the settings file (read once at start-up; a change needs a restart). The section is optional: without it the behaviour is as it always was. A wrong value stops the start and says which one.

<!-- obs:settings -->
| Setting | Default | What it does |
|---|---|---|
| `log_format` | `text` | `text` is the one-line format as before; `json` writes one JSON object per line, for a log collector. |
| `metrics` | `false` | `true` serves Prometheus text at `/metrics`. Off, the address does not exist (404). |
| `tracing` | `false` | `true` sends OpenTelemetry spans. Needs the `otel` extra (below). |
| `otlp_endpoint` | empty | Where the spans go, for example `http://localhost:4318/v1/traces` (OTLP over HTTP). Empty uses the exporter's own default. |
| `service_name` | `erpnext-ai-agents` | The name your tracing tool shows for this service. |
| `pending_hours` | `24` | A request waiting longer than this many hours is a backlog: `doctor` warns, and the first alert rule below uses the same number. |
<!-- /obs:settings -->

Also: the command-line flag `--log-format text|json` (before the command: `erp-agents --log-format json serve`) and the environment variable `ERP_AGENTS_LOG_FORMAT` choose the log format and win over the settings file, in that order. `ERP_AGENTS_METRICS_TOKEN` is the scraper's key (below). A commented example is in `settings.example.yaml`.

## Following one task in the log

Every agent task gets an id when it starts. Every log line the task causes carries it: the task itself, each model call, each tool call, and each request to ERPNext made on the task's behalf. In the text format it is the `[task=...]` after the logger name; in JSON it is `task_id`. This is a real task (the model's tool call and two answers, from the test model):

```text
2026-10-28 19:20:37,961 INFO erp_agents.calls [task=d20a631920]: agent_task_start agent=inventory task=ask
2026-10-28 19:20:38,098 INFO erp_agents.calls [task=d20a631920]: llm_call provider=ollama model=scripted kind=gather ms=1 tokens_in=80 tokens_out=10 outcome=ok
2026-10-28 19:20:38,101 INFO erp_agents.calls [task=d20a631920]: tool_call tool=stock_status via=agent ok=True ms=1
2026-10-28 19:20:38,297 INFO erp_agents.calls [task=d20a631920]: llm_call provider=ollama model=scripted kind=gather ms=0 tokens_in=90 tokens_out=2 outcome=ok
2026-10-28 19:20:38,298 INFO erp_agents.calls [task=d20a631920]: llm_call provider=ollama model=scripted kind=answer ms=0 tokens_in=120 tokens_out=12 outcome=ok
2026-10-28 19:20:38,302 INFO erp_agents.calls [task=d20a631920]: agent_task agent=inventory task=ask status=ok source=model steps=3 tool_calls=1 llm_seconds=0.0 tokens_in=290 tokens_out=24
```

In the JSON format each of those is one object on one line; this is the `tool_call` line:

```json
{"ts": "2026-10-28T13:50:38.364+00:00", "level": "INFO", "logger": "erp_agents.calls", "msg": "tool_call tool=stock_status via=agent ok=True ms=2", "task_id": "681d3cce16", "event": "tool_call", "tool": "stock_status", "via": "agent", "ok": true, "ms": 2}
```

To pull out one task:

```powershell
Select-String -Path logs\app.log -Pattern "task=d20a631920"                                              # text format
Get-Content logs\app.log | ConvertFrom-Json | Where-Object task_id -eq "681d3cce16"                       # json format
```

Where do you get the id? The answer to the call that started the task contains `task_id`, `GET /api/v1/agents/tasks` lists the recent tasks with theirs, and the `agent_task` line is written when a task ends. A request that arrives over HTTP gets a `request_id` the same way (sent back in the `X-Request-ID` header; a sane one you send is kept), and a scheduled job logs its name as `job`. A task started in the background keeps the id of the request that started it. Times in the text format are this computer's local time, as before; JSON times are UTC.

### The lines the assistant writes about what it does

Logger `erp_agents.calls`. Each says what happened, never what was said: no question, answer, prompt, document, item, vendor or person is ever in these lines.

<!-- log:events -->
| Event | When | Fields |
|---|---|---|
| `agent_task_start` | an agent task begins | `agent`, `task` |
| `agent_task` | it ends | `agent`, `task`, `status`, `source` (model or rules), `steps`, `tool_calls`, `llm_seconds`, `tokens_in`, `tokens_out` |
| `llm_call` | one reply from the model | `provider`, `model`, `kind` (gather, answer, repair), `ms`, `tokens_in`, `tokens_out`, `outcome`, and for a failure the class of the error in `error` |
| `tool_call` | one tool call | `tool`, `via` (how it came in: agent, api, mcp-stdio, mcp-http), `ok`, `ms`, and for a refusal why in one word in `refused` |
| `erpnext_request` | one request to ERPNext | `method`, `kind` (the document type or method asked for, never the query), `status`, `ms`, `error` |
| `llm_request` | one HTTP request to a model server | `method`, `kind` (the path, such as `/api/chat`), `status`, `ms`, `error` |
| `job_run` | a scheduled job ended | `job`, `ok`, `error` |
<!-- /log:events -->

A failure is a `WARNING` line with the **class** of the error (for example `ConnectTimeout`), not its text.

### What never reaches a log

Before any line is written, in either format and for every library, a scrubber hides what it knows to be secret. It replaces with `[hidden]` or `[number]`:

- every key, token and password in your configuration and `.env` (the ERPNext key and secret, access keys, hosted-model keys, channel tokens, the hash key, the MCP and metrics tokens), also in their URL-encoded form, and every sign-in token as it is issued;
- anything shaped like a Telegram bot token, an `sk-` or `AKIA` or `ghp_` key, a Meta `EAA` token, a `Bearer ...` or `Authorization:` value, or `key=`, `token=`, `secret=`, `password=` in a web address;
- the phone numbers and chat addresses of your approvers in any written form, any number starting with `+`, and any run of 10 to 15 digits (also written with spaces or dashes).

It makes changes visible only where it recognises something. It cannot know a secret that is not in the configuration and has no recognisable shape. And it errs on the side of hiding: a 10-digit number that is not a phone number (a timestamp, an order number) is hidden too.

## `/health` and `/ready`

They answer different questions, so a monitor can tell "up" from "up but stuck".

- **`GET /health`** is unchanged: the process is answering. Anybody may call it, even through a tunnel; signed in, it also asks ERPNext (`erp_reachable`) and names the channels.
- **`GET /ready`** says whether the assistant can do its work: **200** when all of these hold, **503** when any does not.
  1. *database*: it accepts a write (a real, empty write transaction, which fails the way a real write does on a read-only file, a full disk or a lock held by another process). When another process holds the database's write lock, the check waits up to 5 seconds before it answers 503, so give a probe a timeout of 10 seconds;
  2. *migrations*: the database is the shape this version needs;
  3. *scheduler*: it is running and its maintenance job (every 10 minutes) has run within 35 minutes, or the scheduler started within that time.

  Anybody gets `{"ready": true}` or `{"ready": false}`. A signed-in person also gets each check and why:

  ```json
  {"ready": true, "checks": [{"name": "database", "ok": true, "detail": "accepts a write"}, {"name": "migrations", "ok": true, "detail": "the database is the shape this version needs"}, {"name": "scheduler", "ok": true, "detail": "running; the maintenance job last ran 3 minute(s) ago"}]}
  ```

  When only the webhooks are open to a tunnel (`server.remote_access: webhooks_only`), `/ready` answers 404 to visitors through it, as the rest of the console does.

ERPNext and the model server are **not** part of `/ready`: a probe that restarts a container must not do so because a vendor's server is down. They are in the metrics (below).

## Metrics

Turn them on with `observability: {metrics: true}` and restart. They are Prometheus's text format at `GET /metrics`, written by the application itself (no extra library).

**Who may read them.** Either a bearer token meant for the scraper, or a signed-in administrator. The token is the value of `ERP_AGENTS_METRICS_TOKEN` in `.env`: 24 characters or more, for example `python -c "import secrets; print(secrets.token_urlsafe(32))"`. A stranger gets 401, a signed-in person who is not an administrator 403, and with the feature off the address does not exist. Through a tunnel with `webhooks_only`, it is hidden like the console.

```yaml
# prometheus.yml
scrape_configs:
  - job_name: stock-assistant
    metrics_path: /metrics
    authorization:
      credentials_file: /etc/prometheus/stock-assistant.token     # a file that holds the value of ERP_AGENTS_METRICS_TOKEN
    static_configs:
      - targets: ["127.0.0.1:8100"]
```

**What is in them.** Every label has a small, fixed set of values; nothing personal is a label or a value (no item, vendor, person, phone number or text), and a metric stops accepting new label combinations after 300 (counted in `erp_agents_metric_series_dropped_total`). Two kinds: the *events* are counted in memory as they happen and start again from zero when the application restarts (Prometheus reads that as a counter reset, which is what it expects); the *state* is read from the database when you scrape, so it survives a restart.

<!-- metrics:table -->
| Metric | Type | Labels | What it tells you |
|---|---|---|---|
| `erp_agents_build_info` | gauge | `version` | Which version is running (always 1). |
| `erp_agents_process_start_time_seconds` | gauge | | When this process started (Unix time). |
| `erp_agents_approvals_pending` | gauge | | Requests waiting for a person's answer. |
| `erp_agents_approvals_oldest_pending_age_seconds` | gauge | | How long the longest-waiting request has waited (0 when none). |
| `erp_agents_decisions_total` | counter | `kind`, `outcome` | Closed requests and how they ended (the person's answer, expired, cancelled). |
| `erp_agents_deliveries_total` | counter | `channel`, `status` | Messages to people by channel and status (sent, failed, given_up). |
| `erp_agents_agent_tasks_total` | counter | `agent`, `task`, `status` | Agent tasks and how they ended (ok, fallback, failed), from the database. |
| `erp_agents_llm_tokens_total` | counter | `provider`, `model`, `direction` | Tokens used by agent tasks, from the database. |
| `erp_agents_llm_requests_total` | counter | `provider`, `model`, `kind`, `outcome` | Replies asked of a model, by step and outcome. |
| `erp_agents_llm_request_seconds` | histogram | `provider`, `model` | How long a model took to reply. |
| `erp_agents_llm_tokens_used_total` | counter | `provider`, `model`, `direction` | Tokens used since this process started. |
| `erp_agents_tool_calls_total` | counter | `tool`, `via`, `outcome` | Tool calls (ok, refused, failed). |
| `erp_agents_tool_call_seconds` | histogram | `tool` | How long a tool call took. |
| `erp_agents_http_requests_total` | counter | `target`, `kind`, `outcome` | Requests to ERPNext and to model servers (2xx, 3xx, 4xx, 5xx, error). |
| `erp_agents_http_request_seconds` | histogram | `target` | How long they took. |
| `erp_agents_erpnext_clock_skew_seconds` | gauge | | This computer's clock minus ERPNext's, from the `Date` header of its last answer. |
| `erp_agents_grounding_rejects_total` | counter | `agent`, `task` | Model replies the checks rejected (an invented figure, a guess, a wrong format). |
| `erp_agents_grounding_repairs_total` | counter | `agent`, `task` | Rejected replies the model was given a second chance on. |
| `erp_agents_agent_fallbacks_total` | counter | `agent`, `task`, `reason` | Tasks answered by the plain rules instead (rejected, unavailable, busy). |
| `erp_agents_scheduler_job_runs_total` | counter | `job`, `outcome` | Scheduled jobs that ran (ok, failed, missed). |
| `erp_agents_scheduler_lag_seconds` | gauge | `job` | How late the last run of a job started. |
| `erp_agents_scheduler_last_success_timestamp_seconds` | gauge | `job` | When a job last finished well (Unix time). |
| `erp_agents_metric_series_dropped_total` | counter | | Label combinations refused because a metric held 300 already. |
<!-- /metrics:table -->

## Traces

A trace shows where the time of one task went. It is optional, off by default, and needs packages the base install does not have:

```powershell
pip install "erpnext-ai-agents[otel]"
```

Then set `observability: {tracing: true, otlp_endpoint: "http://localhost:4318/v1/traces"}` and restart. Any receiver of OTLP over HTTP will do (an OpenTelemetry Collector, Jaeger, Tempo). If tracing is on and the packages are missing, the application **refuses to start** and prints the command above, rather than quietly running without the traces you asked for. With tracing off, OpenTelemetry is not even loaded. Credentials for a collector that needs them go in the standard environment variable `OTEL_EXPORTER_OTLP_HEADERS`, not in the settings file.

One agent task is one trace:

<!-- tracing:spans -->
| Span | Child of | Attributes |
|---|---|---|
| `agent.task` | | `agent`, `task`, `task_id` (the id in the log), `status`, `source` |
| `llm.call` | `agent.task` | `provider`, `model`, `kind`, `tokens_in`, `tokens_out`, `outcome` |
| `tool.call` | `agent.task` | `tool`, `via`, `ok`, `refused` |
| `erp.request` | `tool.call` | `method`, `kind` (the document type or method), `status` |
<!-- /tracing:spans -->

Spans carry names, outcomes and counts, as the log lines do, and **never** a question, an answer, a prompt, an item, a vendor, a person, a phone number, a query string or the text of an error. A failure is marked with `error.type` (the class of the error, or the HTTP status) and the span is an error without a message; the usual "record the exception" feature, which would keep its message and stack, is switched off. Values are cut at 120 characters. Spans leave this computer only for the endpoint you named.

## What `doctor` adds

`erp-agents doctor` reads the database file (opened so it cannot be written; fine while the application runs) and, besides its checks of files and settings, tells you:

- **Scheduler**: an instance is running but its maintenance job has not run for 35 minutes (the checks, reminders and expiry are not happening), counted from the later of the last run and the instance's start; and any scheduled job whose last run failed.
- **Disk**: a warning below 500 MB free beside the database (or three times its size), a failure below 50 MB.
- **Clock**: the newest date in the database is more than 5 minutes ahead of this computer's clock. (The live difference from ERPNext's clock is a metric.)
- **Approvals**: a request has waited longer than `pending_hours`.
- **Decision history**: the seal over past decisions still verifies; if not, which request was changed or deleted.

## Three alert rules

Prometheus syntax; load them into your Prometheus (`rule_files:`) and send the alerts wherever you read them. The thresholds are starting points: change them to what is normal for you.

```yaml
groups:
  - name: stock-assistant
    rules:
      - alert: StockAssistantApprovalStuck
        expr: erp_agents_approvals_oldest_pending_age_seconds > 24 * 3600
        for: 15m
        labels:
          severity: warning
        annotations:
          summary: A request has waited more than 24 hours for an answer
          description: Nobody has answered the oldest request, or the message never reached the approver. Keep 24 equal to observability.pending_hours.

      - alert: StockAssistantFallbackRateHigh
        expr: |
          sum(increase(erp_agents_agent_fallbacks_total{reason="rejected"}[6h]))
            / clamp_min(sum(increase(erp_agents_agent_tasks_total[6h])), 1) > 0.25
          and sum(increase(erp_agents_agent_tasks_total[6h])) >= 5
        for: 30m
        labels:
          severity: warning
        annotations:
          summary: The checks rejected the model's answer in more than a quarter of the last tasks
          description: The plain-rules answer was shown instead, so nothing wrong reached a person, but the model is not giving usable answers.

      - alert: StockAssistantErpnextErrors
        expr: sum(increase(erp_agents_http_requests_total{target="erpnext", outcome=~"5xx|error"}[10m])) >= 5
        for: 5m
        labels:
          severity: critical
        annotations:
          summary: Five or more requests to ERPNext failed in ten minutes
          description: ERPNext is down, slow or refusing the connection; stock checks and orders cannot proceed.
```

**An approval stuck for 24 hours.** Someone has to answer, or the message did not arrive.
1. Open the console, *Needs your OK*: the waiting requests are there. Anybody with the approver role can answer them.
2. Look at delivery: `erp_agents_deliveries_total{status=~"failed|given_up"}` rising means the approver never got the message (the WhatsApp template, the Telegram chat id, the address in `approvals.recipients`).
3. Run `erp-agents doctor`. A quiet scheduler means the reminders are not being sent (restart the application), and it lists requests that waited too long.
4. If the person has left, have another approver answer in the console (*Don't order this* closes a request without an order), and take the leaver out of `approvals.recipients`. Every answer is recorded in *History* with the name of whoever gave it.

**A rising share of answers falling back to the rules.** The grounding checks are doing their job (they reject an answer that uses a figure the tools did not return), but the model keeps failing them.
1. Read one rejected task: `GET /api/v1/agents/tasks/<id>?trace=true` as an administrator. Its trace lists the problems the checks found (an invented figure, a guess, a comparison the program should make).
2. Did the model change? A new version of the same model name, a different provider, or a hosted model that is slow. `erp_agents_llm_requests_total{outcome="error"}` shows whether it was reachable.
3. Run the evaluation against the live model and compare with the earlier run: `python -m tools.e2e.agents_eval --suite golden --model llama3.1` ([EVALUATION.md](EVALUATION.md)).
4. Choose another model under *Settings > AI agents*, or switch the agents off: the plain rules keep answering.

**Repeated ERPNext errors.** Five or more failed requests in ten minutes.
1. Is ERPNext up? Open it in a browser. `GET /health` while signed in also asks ERPNext and reports `erp_reachable`.
2. `erp_agents_http_requests_total{target="erpnext"}` by `outcome`: `error` is a connection or timeout (network, ERPNext stopped, TLS); `5xx` is ERPNext itself failing; 4xx is not in this rule, it means the account lacks a permission (`erp-agents erp-check-access`).
3. `erp_agents_erpnext_clock_skew_seconds` far from 0 means the two clocks differ; fix the clock.
4. Nothing is ordered while ERPNext is down: a stock check that could not read it logs that nothing was checked and runs again at its next time (after several failed runs in a row the assistant tells the approvers that the stock monitor is down), and no order is ever made without a person's answer.

## What this does not do

- **Metrics and traces are for one process.** The assistant is a single instance (SQLite and the in-process scheduler), so there is nothing to aggregate across replicas. The counters of events restart from zero with the process; the state read from the database does not. It keeps no history of its own: Prometheus does.
- **No trace context is sent to ERPNext or to the model server**, so their side of a request cannot be joined to a trace. The exporter is best-effort: if the receiver is down, spans are dropped and a warning is logged.
- **The scrubber is a backstop, not a proof.** It hides what the configuration holds and the common shapes of keys and numbers (above). Do not put a secret in a log call.
- **`/ready` and `doctor` cannot see inside a stuck call.** They notice the effect (the maintenance job stopped), not the cause. The cause is in the log, under the job's name.

## For developers: adding to it

- A new log line about what the assistant did goes through `obs.event(name, level, **fields)`; fields are names, numbers and outcomes. Add the event to the table above. The tests compare that table with the events in the code.
- A new metric is registered in `core/metrics.py` with a bounded set of label values and a row in the table above (a test compares the two, names, types and labels). Never use an item, a vendor, a person or free text as a label.
- A new span is `tracing.span(name, **attrs)`; add it to the span table. Attributes are names and counts only. Do not call `record_exception`.
- A new scheduled job is registered through `scheduler.tracked(job_id, fn)` and returns `False` when it failed, so that it is recorded and counted.
- These modules (`obs`, `metrics`, `tracing`, `jobs`, `health`) import no web framework or scheduler library, so they move into a Frappe app unchanged ([DEVELOPING.md](DEVELOPING.md)).
