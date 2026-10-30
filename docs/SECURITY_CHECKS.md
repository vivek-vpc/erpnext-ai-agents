# Security checks: what is tested, by which test, and what it becomes in a Frappe app

This is the register of the security checks of the penetration test of 19 Oct 2026 and the reviews of 24 and 25 Oct 2026, with the tests that now run them in CI, and (since Phase 4 of the hardening review) the checks made against the text that reaches a language model. Nothing here is a new penetration test: it is the same checks, made permanent. [PENTEST_REPORT.md](PENTEST_REPORT.md) says what was found; this page says what keeps it from coming back.

A test named here must exist (`tests/test_security_register.py` reads this page and looks each one up). Where a check has an equivalent in a Frappe app, the last column says what it would be; those lines are a plan written from general Frappe practice and **were not run on a bench**: check them against the version you target (see [FRAPPE_APP_PLAN.md](FRAPPE_APP_PLAN.md)).

## The levels

| Level | Meaning | In a Frappe app |
|---|---|---|
| `public` | answers anybody; nothing it returns is private | a `@frappe.whitelist(allow_guest=True)` method (only the webhooks and liveness would be) |
| `self` | checks its own credential inside the handler (a webhook's signature, the scraper's token) | a guest method that verifies the signature before it reads anything |
| `viewer` | may look: the person can read an Item or a Purchase Order in ERPNext, or holds a viewer access key | `frappe.has_permission(doctype, "read")` in a whitelisted method |
| `approver` | may answer requests and start work that prepares requests: the person can create a Purchase Order | `frappe.has_permission("Purchase Order", "create")` |
| `admin` | may change settings and start runs: Administrator or System Manager | `frappe.only_for("System Manager")` or the Settings DocType's permission rows |

## The routes

`tests/test_routes_sweep.py` compares this table with the routes the application really has (so a new route that is not listed here fails the build), turns away a stranger on every protected row in six ways, and checks that each level gets exactly what the row says: a person below the level is refused with 403, a person at or above it is not.

<!-- routes -->
| Method | Path | Level | Why this level |
|---|---|---|---|
| GET | `/health` | public | liveness; a stranger gets only `{"ok": true}` and no call to ERPNext, a signed-in person gets more |
| GET | `/ready` | public | yes or no for a stranger; the checks only for a signed-in person |
| GET | `/api/v1/auth/mode` | public | the sign-in screen asks it before anybody is signed in |
| POST | `/api/v1/auth/login` | public | the sign-in itself; throttled per address, the same answer for an unknown user and a wrong password |
| POST | `/api/v1/auth/verify` | public | the second step of the sign-in; throttled |
| POST | `/api/v1/auth/logout` | public | ends the caller's own token; nothing to protect |
| GET | `/` | public | redirects to the console |
| GET | `/ui/{path}` | public | the console's files; no data in them, script pinned by hash |
| GET | `/webhooks/{channel_type}` | self | a channel's handshake, checked against the verify token |
| POST | `/webhooks/{channel_type}` | self | a channel's message, checked by signature or secret before it is read |
| GET | `/metrics` | self | exists only when switched on; the scraper's own token or an administrator |
| GET | `/api/v1/auth/me` | viewer | who the caller is |
| GET | `/api/v1/packs` | viewer | which packs are installed and on |
| GET | `/api/v1/settings/schema` | viewer | the shape of the settings |
| GET | `/api/v1/settings/system` | viewer | the system settings (no secret in them) |
| PUT | `/api/v1/settings/system` | admin | changes settings |
| GET | `/api/v1/settings/module/{kind}` | viewer | a module's settings |
| PUT | `/api/v1/settings/module/{kind}` | admin | changes settings |
| GET | `/api/v1/settings/audit` | viewer | who changed which setting |
| GET | `/api/v1/settings/procurement` | viewer | the procurement settings |
| PUT | `/api/v1/settings/procurement` | admin | changes settings |
| GET | `/api/v1/settings/policy/{scope_type}/{scope_key}` | viewer | the stock rules for an item, group or the whole stock |
| PUT | `/api/v1/settings/policy/{scope_type}/{scope_key}` | admin | changes the stock rules |
| DELETE | `/api/v1/settings/policy/{scope_type}/{scope_key}` | admin | removes an override |
| GET | `/api/v1/agents/status` | viewer | whether the AI agents are on |
| GET | `/api/v1/agents/privacy` | viewer | what is sent to a model |
| POST | `/api/v1/agents/check` | admin | contacts a model server on the administrator's behalf |
| GET | `/api/v1/agents/usage` | viewer | tokens used |
| GET | `/api/v1/agents/tasks` | viewer | the agents' task list |
| GET | `/api/v1/agents/tasks/{task_id}` | viewer | one task |
| GET | `/api/v1/agents/brief` | viewer | the latest stock brief |
| POST | `/api/v1/agents/brief` | approver | starts the agent that writes the brief |
| POST | `/api/v1/agents/ask` | viewer | a question to the AI assistant (it may prepare an order request only for an approver) |
| GET | `/api/v1/agents/reviews/{approval_id}` | approver | the second opinion on a request |
| POST | `/api/v1/agents/reviews/{approval_id}` | approver | starts a second opinion |
| GET | `/api/v1/work-orders` | viewer | the work orders |
| GET | `/api/v1/approvals` | approver | the requests waiting for an answer |
| GET | `/api/v1/approvals/{approval_id}` | approver | one request |
| POST | `/api/v1/approvals/{approval_id}/decision` | approver | answers a request: the only way anything is ordered |
| GET | `/api/v1/history` | approver | the decision history |
| GET | `/api/v1/history.csv` | approver | the decision history as a file |
| GET | `/api/v1/history/verify` | admin | checks the seal over the history |
| GET | `/api/v1/history/{approval_id}` | approver | one request's history |
| GET | `/api/v1/assistant/status` | viewer | whether "Ask why" is on |
| POST | `/api/v1/assistant/ask` | approver | "Ask why" about a request |
| GET | `/api/v1/assistant/history/{approval_id}` | approver | earlier answers about a request |
| POST | `/api/v1/chat` | viewer | the chat (it never approves anything) |
| GET | `/api/v1/inventory/items` | viewer | the items |
| GET | `/api/v1/inventory/stock-options` | viewer | the stock columns and warehouses |
| GET | `/api/v1/inventory/alerts` | viewer | the stock alerts |
| GET | `/api/v1/inventory/runs` | viewer | the stock checks that ran |
| POST | `/api/v1/inventory/run` | admin | runs the stock check |
| GET | `/api/v1/inventory/analysis/{item_code}` | viewer | the analysis of one item |
| GET | `/api/v1/inventory/forecast/{item_code}` | viewer | the demand forecast of one item |
| GET | `/api/v1/inventory/slow-stock` | viewer | slow and dead stock |
| GET | `/api/v1/inventory/substitutes/{item_code}` | viewer | approved alternative items |
| GET | `/api/v1/inventory/counts` | viewer | cycle counts |
| GET | `/api/v1/inventory/transfers` | viewer | stock transfer requests |
| POST | `/api/v1/inventory/transfers` | approver | prepares a stock transfer request (a person still answers it) |
| GET | `/api/v1/procurement/proposals` | viewer | the purchase proposals |
| GET | `/api/v1/procurement/preview/{item_code}` | viewer | a preview of an order |
| POST | `/api/v1/procurement/run` | admin | runs the procurement pass |
| GET | `/api/v1/followup/late-deliveries` | viewer | late purchase orders |
| GET | `/api/v1/followup/receipts` | viewer | goods receipt differences |
| GET | `/api/v1/followup/scorecard` | viewer | the vendor scorecard |
| GET | `/api/v1/followup/quotes` | viewer | quote rounds |
| POST | `/api/v1/followup/quotes` | approver | starts a quote round (a person still answers it) |
| POST | `/api/v1/followup/run` | admin | runs the follow-up pass |
<!-- /routes -->

## The checks

Each row is a property of the application that a check of the penetration test or of a later review showed, with the tests that keep it true. A test named here must exist (`tests/test_security_register.py`).
"Source" is where the check came from: T1 to T10 are the probes of the script run on 19 Oct 2026, X0 to X9 its browser checks, "24 Oct" and "25 Oct" the reviews of those days, "Phase n" the hardening review.
The console checks (PT-14) run the real console in a DOM: Node 24 and jsdom 30.1.1, pinned with their hashes in `tests/browser/package-lock.json` and checked with `npm audit` in CI.

<!-- checks -->
| ID | Check | Source | Proved by | In a Frappe app (plan, not run on a bench) |
|---|---|---|---|---|
| PT-01 | The interactive API documentation and the schema are not published | 19 Oct, T1 | `test_the_api_documentation_is_not_published`, `test_the_documentation_can_be_turned_on_by_the_administrator` | Frappe serves no schema to a stranger; the app adds no method that does. |
| PT-02 | A stranger learns only that the service is alive; the detail needs a sign-in | 19 Oct, T1 | `test_health_tells_a_stranger_only_that_the_service_is_alive_and_a_signed_in_person_more`, `test_the_anonymous_health_check_does_not_touch_the_erp`, `test_ready_says_yes_to_anybody_and_gives_the_checks_to_a_signed_in_person` | A guest method that returns a constant; the detailed health is a method with a permission check. |
| PT-03 | Every protected route turns a stranger away (six ways), and a new route cannot be added without deciding who may use it | 19 Oct, T2; Phase 4 | `test_a_stranger_is_turned_away_on_every_protected_route_in_six_ways`, `test_the_register_lists_exactly_the_routes_the_application_has` | `@frappe.whitelist()` needs a login by default; `allow_guest=True` appears on the two webhooks only; the same sweep runs over the app's whitelisted methods. |
| PT-04 | A person below a route's level is refused (403, before the body is read); the level in the table is the one the code asks for | 19 Oct, T3; Phase 4 | `test_each_level_gets_exactly_what_the_register_says`, `test_a_person_who_may_only_look_can_change_nothing`, `test_the_register_agrees_with_what_the_source_asks_for_on_each_route` | `frappe.has_permission(...)` or `frappe.only_for(...)` in each method; the sweep calls each as a user of each role. |
| PT-05 | Browser protections on every answer; the console runs only its own script (pinned by hash) and cannot be framed | 19 Oct, T4, X8, X9 | `test_every_answer_carries_the_browser_protections`, `test_the_console_may_only_run_its_own_script_and_cannot_be_framed`, `test_the_console_page_keeps_the_shape_the_policy_relies_on`, `test_https_through_a_proxy_gets_strict_transport_security` | Frappe sets its own headers and the Desk replaces the console; the app must not weaken them. |
| PT-06 | A request above a size is refused before it is read (webhook 1 MB, sign-in 16 KB, the rest 256 KB) | 19 Oct, T8, T9 | `test_a_huge_body_is_refused_before_it_is_read`, `test_a_body_that_grows_without_declaring_its_size_is_cut_off`, `test_a_huge_question_is_refused_before_it_is_read` | The web server's body limit and Frappe's own; text fields get a length in the DocType. |
| PT-07 | No path under the console reaches a file outside its folder (34 shapes, handed over the way a server hands them) | 19 Oct, T5 | `test_no_traversal_under_the_console_reaches_a_file_outside_its_folder`, `test_the_traversal_check_would_see_a_leak` | Desk assets are served from `public/` by the web server; the app adds no file-serving method (one that reads a file uses Frappe's file API). |
| PT-08 | An access key is accepted only from the server machine; a tunnel or proxy never counts as it; the demo key never leaves it | 19 Oct, T6 | `test_only_a_loopback_connection_addressed_to_a_loopback_name_is_the_server_machine`, `test_any_sign_of_a_proxy_means_it_is_not_the_server_machine`, `test_the_well_known_demo_key_works_on_the_server_machine_and_nowhere_else`, `test_a_short_key_is_refused_from_outside_but_a_long_random_one_is_not`, `test_in_webhooks_only_mode_a_stranger_finds_no_console_no_sign_in_and_no_api`, `test_a_request_through_the_tunnel_is_not_the_server_machine_even_though_it_arrives_from_localhost` | Not applicable: there are no access keys. An integration uses the API key and secret of a user who holds the role it needs. |
| PT-09 | Sign-in: wrong passwords throttled; an unknown user looks like a wrong password; log lines cannot be forged; one person cannot sign others out; tokens are long, unique and end at sign-out | 19 Oct, T7, T10 | `test_wrong_passwords_are_throttled_and_the_right_one_is_not_tried_while_blocked`, `test_an_unknown_user_and_a_wrong_password_are_told_the_same`, `test_text_typed_into_the_sign_in_box_cannot_forge_log_lines`, `test_one_account_cannot_push_everybody_else_out`, `test_session_tokens_are_long_unique_and_unguessable`, `test_signing_out_ends_the_session`, `test_a_sign_in_cannot_be_made_with_the_wrong_kind_of_value` | The app has no sign-in of its own: Frappe's login, session and rate limit apply, and the Marketplace asks that base behaviour is not overridden. |
| PT-10 | Webhooks: no, wrong, empty, old-style or prefix-less signatures are refused; the handshake needs the verify token; with no secret nothing is accepted | 19 Oct, T8 | `test_a_webhook_without_a_good_signature_is_refused`, `test_the_handshake_needs_the_verify_token`, `test_a_channel_that_is_not_switched_on_cannot_be_used_and_an_unknown_one_does_not_exist`, `test_a_webhook_with_no_secret_configured_accepts_nothing`, `test_a_forged_whatsapp_webhook_is_refused_and_changes_nothing`, `test_a_forged_telegram_webhook_is_refused_and_changes_nothing` | The only two `allow_guest=True` methods; each verifies the signature before it reads anything (Frappe's Semgrep rule `guest-whitelisted-method` flags any other). |
| PT-11 | Hostile strings (SQL, traversal, template syntax, CRLF, NUL, 5 KB, invisible characters) in every path and query parameter and every body field cause no error and leak nothing | 19 Oct, T9; Phase 4 | `test_hostile_strings_in_every_path_and_query_parameter_cause_no_error_and_leak_nothing`, `test_hostile_strings_in_every_text_field_of_every_request_body_cause_no_error_and_leak_nothing`, `test_the_leak_check_itself_sees_a_leak` | Parameters are bound by `frappe.db` and `frappe.qb`; Frappe's Semgrep rule `frappe-sql-format-injection` guards it. |
| PT-12 | Odd input: nested JSON, a 2 MB question, a wrong type; an error never repeats what the caller sent; a bad schedule, time zone or unknown setting is refused and changes nothing | 19 Oct, T9; Phase 4 | `test_deeply_nested_json_is_refused_cleanly`, `test_a_request_that_does_not_fit_is_refused_without_repeating_what_the_caller_sent`, `test_bad_settings_are_refused_and_change_nothing`, `test_a_time_zone_must_be_one_the_database_knows`, `test_a_schedule_or_zone_typed_into_any_settings_section_is_checked` | DocType field validation (Select options, `validate`); Frappe's own error answers. |
| PT-13 | Odd HTTP methods get nothing; a forged Host is not reflected into a redirect; no site may call the API from a browser | 19 Oct, T10 | `test_odd_methods_get_nothing_from_a_protected_route`, `test_a_forged_host_header_is_not_reflected_into_a_redirect`, `test_the_api_names_no_site_that_may_call_it_from_a_browser` | `allow_cors` stays off. |
| PT-14 | Hostile text from ERPNext (item and vendor names, a person's full name, the chat and its replay) is inert on every page of the console | 19 Oct, X0 to X7; Phase 4 | `test_hostile_text_is_inert_on_every_page_of_the_console`, `test_the_escape_function_escapes_the_five_characters_that_matter`, `test_the_script_uses_nothing_that_runs_text_as_code`, `test_no_free_text_field_is_put_into_markup_without_esc`, `test_the_status_word_of_an_answer_is_escaped_when_it_has_no_label_of_its_own` | The Desk escapes what it renders; a page script that builds HTML escapes with Frappe's helpers; the DOM test is rewritten against the Desk pages. |
| PT-15 | A hosted provider's key goes only to its own https host and never after a redirect; "check connection" cannot reach this machine or the private network | 24 Oct | `test_the_key_is_attached_only_at_the_providers_own_https_host`, `test_a_redirect_from_the_provider_is_not_followed_and_the_key_never_reaches_the_second_host`, `test_check_never_contacts_this_machine_or_the_private_network_for_a_hosted_provider`, `test_a_custom_address_never_receives_a_key_even_when_one_is_set` | The key is a Password field of the Settings DocType; outgoing calls use the same `core/netguard.py`. |
| PT-16 | A request that carries a credential (ERPNext, WhatsApp, Telegram, a model, the MCP client) does not follow a redirect | 24 Oct | `test_the_erpnext_client_does_not_follow_a_redirect_with_its_api_key`, `test_whatsapp_and_telegram_do_not_follow_redirects_with_their_tokens`, `test_the_mcp_client_does_not_follow_redirects_either`, `test_each_request_refuses_redirects_by_itself_even_on_a_plain_session` | The same `core/netguard.py` moves unchanged. |
| PT-17 | The MCP endpoint answers the server machine only by default; Host and Origin are checked even with a valid token; it does not pretend to be OAuth | 24 Oct | `test_a_valid_token_with_an_unexpected_host_is_refused`, `test_a_valid_token_with_an_unexpected_origin_is_refused`, `test_over_http_the_endpoint_answers_the_server_machine_only_unless_remote_is_switched_on`, `test_the_endpoint_does_not_pretend_to_be_an_oauth_protected_resource` | A whitelisted method behind Frappe's OAuth 2 provider (package 7 of FRAPPE_APP_PLAN.md). |
| PT-18 | A number typed into a failed tool call is never quoted as if a tool had returned it | 25 Oct | `test_numbers_typed_into_a_failed_call_are_not_known`, `test_an_answer_built_on_a_rejected_call_is_refused_end_to_end` | `core/agents/runtime.py` moves unchanged. |
| PT-19 | A plain question is never offered the order tool, and a viewer never is | 25 Oct | `test_a_plain_question_never_offers_prepare_order_request`, `test_an_explicit_order_instruction_from_an_approver_does_offer_it_and_a_viewer_never_gets_it` | The tool registry checks the role on every call with `frappe.has_permission`. |
| PT-20 | Text from outside is cleaned and cut before it reaches a tool error, a log line or the tool-call record | 25 Oct | `test_untrusted_text_loses_control_and_invisible_characters_and_is_capped`, `test_a_tool_error_never_echoes_control_characters_or_long_text`, `test_the_tool_call_record_holds_no_control_characters`, `test_log_lines_from_strangers_are_cleaned` | The same helper (`core/common.py: untrusted`). |
| PT-21 | No key, token or phone number reaches a log line | Phase 2 | `test_no_token_key_or_phone_number_reaches_the_logs` | The same scrubber; nothing secret is written to Frappe's Error Log. |
| PT-22 | The program refuses to start with a well-known key unless the demo flag is set | Phase 0, F-11 | `test_the_program_will_not_start_with_a_well_known_key_unless_the_demo_flag_is_set` | Not applicable: there is no key file. |
| PT-23 | A message delivered twice is answered once; a failing service is not asked again and again; a flood is limited with a 429 and the seconds to wait | Phase 3 | `test_a_whatsapp_message_delivered_twice_is_answered_once`, `test_a_dead_erpnext_is_asked_five_times_and_then_not_at_all`, `test_the_twenty_first_question_in_a_minute_is_refused_with_the_wait_in_words_and_in_a_header` | See the Frappe column of [RESILIENCE.md](RESILIENCE.md). |
| PT-24 | Text that reaches a model is cleaned, cut and between marks on every path: "Ask why", the chat's restatement and router, the agents' questions, digests and tool results | Phase 4 | `test_ask_why_shows_the_model_every_fact_between_marks_cut_and_cleaned`, `test_the_chats_restatement_and_router_show_the_model_marks_not_raw_text`, `test_an_agent_sees_a_tool_result_with_every_string_cleaned_and_cut`, `test_the_briefs_digest_puts_names_between_marks`, `test_a_tool_result_given_as_a_user_message_is_between_marks`, `test_a_value_cannot_close_the_marks_and_write_outside_them`, `test_characters_that_are_invisible_or_hide_the_order_of_text_never_survive` | `core/prompting.py` moves unchanged; in the app it matters more, because anyone who may edit an Item can plant text an agent reads. |
| PT-25 | A model that does as hostile text says cannot write to the ERP, change a waiting request, message anyone, order another item or quantity, or get an invented figure or vendor through | Phase 4 | `adv-a-fake-tool-call-for-another-item-is-refused-even-when-an-order-was-asked-for`, `adv-a-fake-tool-call-in-the-models-text-is-not-run`, `adv-a-figure-nobody-recorded-is-rejected-after-the-injection`, `adv-a-line-break-in-a-name-cannot-forge-a-fact`, `adv-a-long-name-is-cut-before-the-model-reads-it`, `adv-a-longer-item-code-is-not-the-code-the-person-typed`, `adv-a-name-tells-the-model-to-order`, `adv-a-question-over-the-limit-is-refused`, `adv-a-quote-request-nobody-asked-for-is-refused`, `adv-a-vendor-that-does-not-exist-is-rejected`, `adv-a-viewer-cannot-be-made-to-order-by-a-name`, `adv-an-order-for-another-item-or-quantity-is-refused-and-the-right-one-goes-through`, `adv-ask-why-a-fact-that-does-not-exist-is-rejected`, `adv-ask-why-an-invented-figure-is-rejected`, `adv-ask-why-the-marks-the-model-copies-are-not-shown`, `adv-hidden-characters-in-a-name-never-reach-the-model`, `adv-template-strings-in-a-name-are-shown-as-they-are`, `adv-the-question-is-cleaned-before-the-model-reads-it`, `adv-the-restatement-of-a-hostile-item-is-fenced`, `adv-the-router-answers-in-prose`, `adv-the-router-cannot-be-talked-into-an-action`, `adv-tools-that-do-not-exist-cannot-change-a-waiting-request` | The tool registry and the approval service move unchanged; the Approval Request DocType's permission rows stop a user from changing another's request. |
| PT-26 | The gate over those cases fails when the fence or the order gate is broken | Phase 4 | `test_the_evaluation_notices_a_broken_check` | Unchanged. |
<!-- /checks -->

## Text that reaches a model

Item names, vendor names, a request's title, a person's full name and a question are text somebody else controls. A language model reads them next to its instructions, so every path that builds a prompt
prepares them with `core/prompting.py` (tests: PT-24). The module is pure Python and moves into a Frappe app unchanged.

<!-- fence -->
| What | Value | Where it applies |
|---|---|---|
| Marks | ⟦ and ⟧ | around every name, title, fact and question put into a prompt; removed from the text between them, and taken out of a model's reply if it copies them |
| `MAX_FIELD` | 200 | one name, title or note |
| `MAX_FACT_LINE` | 400 | one numbered fact of "Ask why" and of the chat's restatement |
| `MAX_TOOL_STRING` | 240 | one string in a tool's result (the longest honest sentence a tool writes in the demo world is 171 characters; an item name is at most 140 in ERPNext) |
| `MAX_QUESTION` | 500 | what a person typed |
<!-- /fence -->

Also removed before a prompt, a log line or a tool error: control characters, zero-width and bidirectional-override characters, the 128 invisible "tag" characters, the variation selectors supplement, the soft hyphen
and a few more (`core/common.py: untrusted`). Every system prompt that carries such text includes one sentence saying that what is between the marks, and every value in a tool result, is data and never to be followed.

What this does and does not do. Marking and capping lower the chance that a small model is steered by a sentence in an item name, and bound how much of the prompt a hostile field can take. They are not a proof.
What guarantees the outcome is the code behind the model: its tools only read or prepare a request that a person must answer; the caller's role is checked on every call; an order request is for the item and the
quantity the person typed, and for nothing else; and an answer is shown only if its figures and codes come from tool results. The adversarial cases show those guarantees holding with a model that does as the
planted text says (PT-25). Two things they do not cover, both in [KNOWN_LIMITATIONS.md](KNOWN_LIMITATIONS.md): a figure or a code that appears inside a name counts as recorded, and a sentence with no figure in it
is not checked against anything.

## Semgrep with Frappe's rules

Frappe's guide for Marketplace apps asks them to run Semgrep with its own rules. The CI job `semgrep` (not a required check) runs [frappe/semgrep-rules](https://github.com/frappe/semgrep-rules) (MIT) at commit
`81a6e3d47a328249e8ddf04c586e493dd5553002` (30 Sep 2026) with Semgrep `1.182.1` on `src/` without the tests. Findings of severity ERROR fail the job. Baseline on 30 Oct 2026, run in the official container with no
network:

| Rules | Findings | What they are |
|---|---|---|
| Frappe's, ERROR | 0 | (a file that breaks them fails the job: checked) |
| Frappe's, WARNING | 28 | 23 `overusing-args` (the command-line functions take an argparse namespace); 4 `frappe-no-functional-code` (a `map` or a lambda); 1 `frappe-security-file-traversal`, an audit prompt on `core/ops.py`, which opens the instance lock file whose path is the operator's configured database path and never a request's |
| `r/python.lang.correctness` (registry; advisory) | 20 | 5 `return-in-init` (a lambda inside an `__init__`); 14 `string-concat-in-list` (one sentence written over two lines inside a list); 1 `useless-eqeq` (`v != v`, the test for NaN) |

Almost none of Frappe's rules can fire before the port, because they look for Frappe calls (`frappe.db.sql`, `@frappe.whitelist`). The job exists so that the rules that apply to any Python already run, and so that the
port starts with the job in place.

## What is not tested, and why

* **The Content-Security-Policy in a real browser, and `<img onerror>` actually firing.** The DOM test runs in jsdom, which does neither. It inspects the page for what a payload would leave behind, and
  `tests/test_hardening.py` checks the header and the shape of the page the policy relies on. The 19 Oct run in Edge is the only run in a real browser.
* **A value put into a variable and then into markup.** The console lint sees property chains named like free text put straight into a template; the DOM test and review cover the rest.
* **A real model against hostile text.** The adversarial cases use a stand-in that does as the text says. They prove the code around the model, not how a particular model behaves.
* **WhatsApp, Telegram, Meta, Cloudflare, ERPNext and real two-step verification** are not contacted by any test (a fake transport and a stand-in ERPNext).
* **Guessing one account's password from many addresses** is accepted, with its reason, in [PENTEST_REPORT.md](PENTEST_REPORT.md).
* **An item whose name is the beginning of another item's name**, when the person types the name rather than the code (KNOWN_LIMITATIONS.md).

## Adding a check

1. Write the test first and watch it fail on the code that has the problem; if it passes at once, the check was already handled, and the register says so by naming it.
2. Add a row above with the test's name (and the Frappe column: what the same property is in a bench app).
3. A new route needs a row in the table of routes; a new way for text to reach a model needs `core/prompting.py` and a case in `tests/evals/cases/15_adversarial.yaml`.
4. Break the code on purpose and see the test fail (the mutation lists in the commit messages of Phase 4 show how).
