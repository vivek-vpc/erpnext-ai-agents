# When something fails: ERPNext, the model, a message that comes twice, a stop at the wrong moment

This page says what the Stock Assistant does when ERPNext or the language model stops answering, when a model sends rubbish, when WhatsApp or Telegram delivers a message twice, when someone asks too often, and when the program itself stops between "you pressed Create order" and "the answer was written down". Each statement names the test that proves it; a test checks that the names are real.

Nothing here changes what the assistant decides. It never orders by itself, it never invents a figure, and a person still answers every request.

## Settings

The `resilience:` section of the settings file (optional; read at start-up; a wrong value stops the start and names the key). The example is in `settings.example.yaml`.

<!-- resilience:settings -->
| Setting | Default | What it does |
|---|---|---|
| `erpnext_failures` | `5` | Failed requests to ERPNext in a row after which the application stops asking it for a while. |
| `erpnext_pause_seconds` | `30` | How long it stops before it tries one request again. |
| `model_failures` | `3` | The same for a language model. |
| `model_pause_seconds` | `60` | How long before the model gets one trial request. |
| `chat_per_minute` | `20` | Questions to the assistant per person per minute. |
| `channel_chat_per_minute` | `6` | Messages from one WhatsApp or Telegram chat per minute that may reach the assistant. |
| `model_per_minute` | `6` | Actions per person per minute that start a language-model task. |
| `inbound_memory_days` | `7` | How long the ids of channel messages are remembered. |
<!-- /resilience:settings -->

## A service that keeps failing: the circuit breaker

Asking a server that is down makes every person wait for the timeout (30 seconds for ERPNext, minutes for a model), again and again. So the application watches the answers. After `erpnext_failures` failed requests in a row it **stops asking** for `erpnext_pause_seconds`: every call fails at once with "ERPNext is not answering ... the next try is allowed in N seconds", and nothing is sent. After the pause, **one** request goes out as a trial. If it is answered, the application asks as usual again; if not, the pause starts over. The model has its own breaker per provider (a hosted provider and the Ollama on this computer are separate).

What counts as the service being down: no connection, a timeout, a broken connection, and an answer of 500 or above. What does **not**: any other answer. A 404 is a missing record, a 401 a wrong key, a 429 a quota the provider has run out of. The service is up and said no, so the circuit stays closed.

The change of state is reported **once**: one log line (`circuit_change`), the gauge `erp_agents_circuit_state` and the count `erp_agents_circuit_transitions_total` ([OBSERVABILITY.md](OBSERVABILITY.md)). Requests that were not sent are only counted (`erp_agents_circuit_refused_total`). A signed-in `GET /health` shows each dependency as `closed` or `open`. **Check connection** (Settings > AI agents) is a trial a person asked for: it goes out even during the pause, and a good answer closes the circuit, so nobody waits for the pause after restarting Ollama.

| Proved by | |
|---|---|
| `test_a_dead_erpnext_is_asked_five_times_and_then_not_at_all`, `test_a_model_server_that_is_down_is_tried_three_times_and_then_left_alone` | the limits, and that nothing is sent while open |
| `test_slow_and_failing_answers_count_and_a_missing_or_refused_record_does_not`, `test_a_hosted_providers_quota_answer_never_opens_it_but_an_outage_does` | what counts |
| `test_check_connection_goes_out_even_while_open_and_a_good_answer_closes_it` | Check connection |
| `test_the_signed_in_health_answer_names_the_state_of_each_dependency` | `/health` |
| `test_the_sign_in_and_the_client_share_what_they_learn_about_erpnext` | one breaker per dependency |

### What each part does while ERPNext is down

<!-- resilience:down-erpnext -->
| Part | What happens | Proved by |
|---|---|---|
| The stock check | It is recorded as failed at once, with the reason; nothing is invented. After several failed runs in a row the existing "stock monitor is down" notice is sent. | `test_the_stock_check_is_recorded_as_failed_at_once_and_says_why`, `test_the_owner_is_told_after_n_failures_in_a_row_and_only_once` |
| A question to the assistant | "I could not read the data just now ... Nothing was changed. Please try again in a moment." No figure is given. | `test_the_assistant_never_invents_a_figure_while_erpnext_is_down_and_stops_asking_it` |
| Pressing Create order | Refused before anything is sent: nothing is created, the request stays waiting, and pressing again later works. | `test_a_creation_is_refused_before_anything_is_sent_while_it_is_open` |
| Signing in with an ERPNext account | "ERPNext cannot be reached right now", at once. | `test_signing_in_while_erpnext_is_down_stops_asking_it_and_says_it_cannot_be_reached` |
<!-- /resilience:down-erpnext -->

### What each part does while the model is down

<!-- resilience:down-model -->
| Part | What happens | Proved by |
|---|---|---|
| An agent question | "The AI assistant is not available just now." (source: the plain rules). The model is tried three times, then left alone for a minute. | `test_the_agents_fall_back_to_the_rules_while_the_model_is_down_and_stop_asking_it` |
| A question the rules cannot place, in the chat | The person gets the help text or "not enough information"; the helper failing is never shown as an error. | `test_a_question_the_rules_cannot_place_never_fails_because_the_helper_model_misbehaves` |
| Everything the rules answer on their own (stock, vendors, orders, what is waiting) | Unaffected. | `test_the_model_limit_is_one_count_for_all_four_actions_and_leaves_the_chat_alone` |
<!-- /resilience:down-model -->

## Creating a draft: a lost answer never makes a second draft

Every draft (purchase order, requisition, stock transfer) is made with the id of the request that led to it written on it. If ERPNext stores the draft and the answer is lost, the next try finds the draft by that id instead of making another.

<!-- resilience:create -->
| What goes wrong | What happens | Proved by |
|---|---|---|
| The connection drops or times out **after** ERPNext stored the draft | The person is told it could not be confirmed, the request stays waiting; the second press records the draft that exists. One draft. | `test_a_timeout_or_a_dropped_connection_after_the_draft_was_stored_leaves_one_draft`, `test_approving_an_order_whose_answer_was_lost_is_rolled_back_and_the_second_press_makes_no_second_draft` |
| ERPNext answers 500 or 503 | Nothing is stored; the person is told; the retry makes the one draft. | `test_a_5xx_stores_nothing_says_so_and_the_retry_makes_the_one_draft`, `test_approving_an_order_when_erpnext_has_a_5xx_changes_nothing_and_the_retry_works` |
| ERPNext refuses with a reason | The reason is passed on. | `test_a_refusal_with_a_reason_passes_the_reason_on_and_stores_nothing` |
| ERPNext stores the draft and its answer is **cut off** (or has no name) | The draft is looked up by its id and recorded. (Before Phase 3 the person saw an error although the draft existed.) | `test_an_answer_that_is_cut_off_does_not_hide_a_draft_that_was_stored`, `test_approving_an_order_whose_answer_is_cut_off_is_recorded_with_the_draft_that_exists` |
| The same, with no id to look it up by, or no draft found | It says a draft may exist and to check ERPNext before trying again; it never claims nothing was made. | `test_an_unreadable_answer_to_a_request_without_a_reference_says_a_draft_may_exist`, `test_a_cut_off_answer_whose_draft_cannot_be_found_is_reported_as_not_known` |
| A requisition or a stock transfer | The same. | `test_a_requisition_whose_answer_was_lost_is_not_made_twice`, `test_a_stock_transfer_whose_answer_was_lost_or_cut_off_is_not_made_twice` |
<!-- /resilience:create -->

## The program stops between "approved" and "written down"

Pressing Create order asks ERPNext to make the draft and **then** writes the answer to the database. If the program stops in between, ERPNext has the draft while the database still says "waiting". Left alone, that request expired, the reorder was asked again as a new request with a new id, and approving it made a **second** draft (reproduced before this was fixed).

Now the next start finishes it, once. At start-up, and at the beginning of the housekeeping job (every ten minutes, before requests are expired), each waiting request is checked: does ERPNext already have the draft made for it? If so, the answer is recorded as it was given, with the approver whose name the application wrote on the draft; the request is closed (so it cannot expire); nothing is created, removed or looked up again; and the people who were sent the request are told once, with one sentence saying the application had stopped before it could write the answer down (one log line, `decision_recovered`, and one count, `erp_agents_decisions_recovered_total`, say that it happened). A draft whose approver cannot be read is left alone and reported once in the log. If ERPNext cannot be asked, the next pass tries again.

Covered: purchase orders, requisitions and stock transfers. **Not** covered: changes to an existing order (a changed quantity or vendor). The check needs the custom fields `erp-agents erp-setup` creates, and it asks ERPNext about every waiting request every ten minutes.

<!-- resilience:recovery -->
| Proved by | |
|---|---|
| `test_a_restart_finishes_an_order_that_was_created_but_never_recorded`, `test_a_requisition_is_finished_the_same_way`, `test_a_stock_transfer_is_finished_the_same_way` | it is finished, and nothing is created again |
| `test_the_approvers_are_told_what_happened_once`, `test_it_is_one_log_line_and_one_count_and_a_second_pass_adds_nothing` | told once |
| `test_the_request_is_finished_before_it_can_expire_so_no_second_draft_follows`, `test_the_housekeeping_job_runs_the_recovery_first`, `test_serve_runs_the_recovery_before_the_scheduler_starts` | when it runs |
| `test_when_no_draft_exists_nothing_is_changed_and_the_button_still_works`, `test_an_unreachable_erpnext_leaves_it_for_the_next_pass`, `test_a_draft_whose_approver_cannot_be_read_is_left_alone_and_reported_once` | when it cannot |
| `test_a_person_pressing_the_button_and_the_recovery_never_record_the_answer_twice`, `test_a_stale_second_pass_cannot_record_the_answer_twice` | no double answer |
<!-- /resilience:recovery -->

## A message that a channel delivers again

WhatsApp and Telegram deliver a message again when our answer was slow or failed, and Telegram hands an update out again after a restart that lost its place. A button press was already single-use; a typed message was answered twice. Now each message is recognised by the id its channel gave it (WhatsApp's message id, Telegram's update id), remembered for `inbound_memory_days`, and a second delivery is ignored in silence: no second answer, no "already answered" message, and a typed "change the quantity to 20" is not applied twice. A message whose handling **failed** is given back, so a delivery that comes again is tried again. A message with no id is handled as before. The log line (`inbound_duplicate`) and the count (`erp_agents_inbound_duplicates_total`) say which channel, never the message or its id.

<!-- resilience:inbound -->
| Proved by | |
|---|---|
| `test_a_whatsapp_message_delivered_twice_is_answered_once`, `test_a_telegram_update_delivered_twice_is_answered_once`, `test_the_same_update_handed_out_again_after_a_restart_is_recognised` | typed messages |
| `test_a_button_pressed_message_delivered_again_is_ignored_in_silence`, `test_a_telegram_button_delivered_twice_does_not_answer_twice` | buttons |
| `test_a_message_whose_handling_failed_may_be_delivered_again_and_tried_again`, `test_a_message_without_an_id_is_handled_as_before` | failures and messages without an id |
| `test_ids_are_forgotten_after_the_set_number_of_days_by_the_housekeeping_job` | retention |
<!-- /resilience:inbound -->

## Asking too often

One set of limits for the whole application, per person per minute, with a count (`erp_agents_rate_limited_total`) and a log line (`rate_limited`, never who asked or what):

<!-- resilience:limits -->
| Scope | Default | Applies to |
|---|---|---|
| `chat` | 20 | questions to the assistant, in the console and through the API |
| `channel_chat` | 6 | messages from one WhatsApp or Telegram chat |
| `model` | 6 | actions that start a language-model task: Ask why, an agent question, a brief, a review, and the helper that restates an answer |
<!-- /resilience:limits -->

A refusal says how long to wait: "That is a lot of questions in a minute. Please wait 13 seconds." On the API it is HTTP 429 with a `Retry-After` header. A brief that the scheduler writes in the morning is not a person asking and is not limited. A question the rules answer is not held up when the model count is used up. Logging in has its own throttle for wrong passwords.

Proved by `test_the_twenty_first_question_in_a_minute_is_refused_with_the_wait_in_words_and_in_a_header`, `test_an_action_that_starts_a_model_task_is_limited_to_six_a_minute`, `test_the_seventh_message_from_one_phone_in_a_minute_gets_a_polite_stop_with_the_wait`, `test_the_model_limit_is_one_count_for_all_four_actions_and_leaves_the_chat_alone`, `test_a_refusal_is_counted_and_logged_without_saying_who`.

## A model that sends rubbish

A model server, or something pretending to be one, can send any JSON. Whatever it sends, a reply of the wrong overall shape is an error ("unexpected response") and the odd part of a good reply is dropped, never a crash:

<!-- resilience:rubbish -->
| It sends | Result | Proved by |
|---|---|---|
| Nothing, HTML, half a JSON document, `null`, a list, a message that is text | An error; an agent question gets "The AI assistant is not available just now." | `test_a_body_that_is_not_a_reply_is_an_error_and_never_an_exception_of_another_kind`, `test_an_agent_question_to_a_model_that_is_down_slow_or_sending_rubbish_gets_the_honest_answer` |
| Tool calls that are not calls, arguments that are not JSON, content that is not text, token counts that are not numbers | Dropped or made harmless: no call, empty arguments, no text, zero tokens. | `test_odd_parts_of_a_reply_are_dropped_or_made_harmless_and_never_crash_the_client`, `test_token_counts_that_are_not_numbers_are_zero_and_never_an_error` |
| A tool that does not exist, or the order tool without a person asking | Refused by the tool layer; nothing is ordered or requested. | `test_a_model_that_asks_for_a_tool_that_does_not_exist_or_with_wrong_arguments_is_refused_by_the_tool_layer` |
| Prose where the format was asked for | Rejected, repaired once, then the plain-rules answer. | `test_a_model_that_answers_in_prose_instead_of_the_format_is_rejected_twice_and_the_rules_answer` |
| The same, from OpenAI, Claude or Gemini | The same. | `test_the_hosted_clients_turn_a_reply_of_the_wrong_shape_into_an_error_too` |
| A reply that is too slow | An error after the configured timeout (300 seconds for the agents, 240 for the helper). | `test_a_model_that_is_too_slow_is_an_error_not_a_wait_for_ever`, `test_every_request_carries_the_configured_timeout` |
<!-- /resilience:rubbish -->

## In the Frappe app

These are the pieces that sit at the edges, so each has a plain Frappe counterpart. This is a **plan**: it is written from general Frappe practice and has not been run on a bench, and every name below must be checked against the version targeted ([FRAPPE_APP_PLAN.md](FRAPPE_APP_PLAN.md)).

<!-- resilience:frappe -->
| Here | Module | In a Frappe app |
|---|---|---|
| Circuit breaker for the model clients | `core/resilience.py` (rules), `core/httpguard.py` (the `requests` edge) | Kept for hosted models, with the state in the site's cache (`frappe.cache()`) through the small store the breaker already uses, so every worker learns together. |
| Circuit breaker for ERPNext | the same | Not needed: the app runs inside the site, there is no remote ERPNext to stop asking. |
| Per-person rate limits | `core/resilience.py` (`RateLimiter`, `Limits`) | Frappe's own rate limiter on the whitelisted methods (`frappe.rate_limiter`), behind the same interface. |
| Ids of delivered messages | `core/approvals/inbound_ledger.py`, table `inbound_seen` | A DocType whose name is the channel and the id; `claim` is an insert that fails when the name exists. Webhook methods call it first. |
| Finishing an answer after a stop | `ApprovalService.reconcile_pending`, `KindHandler.reconcile`, `find_by_reference` | Not needed: the draft and the approval are written in one database transaction, so there is no moment in which one exists without the other. |
| Model tasks that run for minutes | `core/agents/service.py` (threads) | `frappe.enqueue` on the long queue with a job id that stops the same task being queued twice. |
<!-- /resilience:frappe -->

## What this does not do

- **The circuit breaker and the rate limits live in the memory of the one running process.** They start again after a restart, which is harmless: the breaker learns again in a few requests, and a limit gives a person at most one extra minute of questions. The ids of delivered messages and the answers are in the database and survive a restart.
- **Finishing an answer does not cover a changed order**, and relies on the application's own fields in ERPNext (`erp-agents erp-setup`).
- **A message is recognised only if its channel gave it an id**, and only for `inbound_memory_days`.
- **A slow model that does answer is never cut off by the breaker**: only failures count. Its own timeout is the limit.
- **Nothing here tells the approvers that ERPNext is down.** It is in the log, the metrics and the signed-in `/health`; the existing "stock monitor is down" notice still goes out after repeated failed runs.

## For developers

- A new dependency reached over HTTP gets its breaker by creating its session through `guard_session(obs.observe_session(session, "x"), "name")` (`core/httpguard.py`). One name per dependency; the breaker's limits come from `resilience.Breakers`.
- A new kind of request whose answer creates something elsewhere should register `KindHandler(reconcile=...)`: given a waiting request, return the answer already given (found in the other system by the id the thing was made with) or `None`, and raise if the other system cannot be asked. The answer carries the thing it found in `params`, so the handler records it without making it again.
- A new kind of ask that should be limited gets a scope in `Limits.per_minute` and a line in the table above. A new action that starts a model task calls `model_limit(actor)` first.
- A new channel gives each `InboundEvent` the `external_id` its provider assigned; `process_inbound` does the rest.
- The test double for the faults above is `tests/fake_erpnext.py`: a small ERPNext behind a `requests` adapter, with one scripted fault per document POST.
