"""docs/OBSERVABILITY.md says what the code does (Phase 2 of the hardening brief). Its tables are between marker comments and are compared here with the metrics registry, the
events and spans the code writes, the settings and the numbers in the code, so a metric, event, span or setting that is not described fails the build, and so does a description
of one that is gone. The alert rules are real YAML that use real metrics and real labels."""
import ast
import re
from pathlib import Path

import pytest
import yaml

from erp_agents.core import doctor, jobs, metrics, obs, tracing
from erp_agents.core.api.hardening import MIN_KEY_LENGTH
from erp_agents.core.erp.mock import MockERP
from erp_agents.core.storage import SCHEMA_VERSION

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src" / "erp_agents"
DOC = ROOT / "docs" / "OBSERVABILITY.md"
TEXT = DOC.read_text(encoding="utf-8")


def table(name):
    """The rows of the table between <!-- name --> and <!-- /name -->, as lists of cells (the header and the rule under it left out)."""
    found = re.search(rf"<!-- {name} -->(.*?)<!-- /{name} -->", TEXT, re.S)
    assert found, f"{DOC.name} has no <!-- {name} --> table"
    lines = [l for l in found.group(1).splitlines() if l.strip().startswith("|")]
    return [[c.strip() for c in l.strip().strip("|").split("|")] for l in lines[2:]]


def names_in(cell):
    return re.findall(r"`([^`]+)`", cell)


@pytest.fixture
def metrics_on(make_app, clock):
    metrics.REGISTRY.reset()
    app = make_app(erp=MockERP(today=clock().date()), cfg={"observability": {"metrics": True}})
    yield app
    metrics.configure(False)
    metrics.REGISTRY.reset()
    metrics.REGISTRY.set_collector("database", None)


# ------------------------------------------------------------------ metrics
def test_the_metric_table_lists_every_metric_with_its_type(metrics_on):
    rendered = dict(re.findall(r"^# TYPE (\S+) (\w+)$", metrics.REGISTRY.render(), re.M))
    documented = {names_in(r[0])[0]: r[1] for r in table("metrics:table")}
    assert documented == rendered, (sorted(set(rendered) - set(documented)), sorted(set(documented) - set(rendered)))


def test_the_labels_in_the_metric_table_are_the_labels_of_the_metrics(metrics_on):
    documented = {names_in(r[0])[0]: names_in(r[2]) for r in table("metrics:table")}
    for name, metric in metrics.REGISTRY._metrics.items():
        assert documented[name] == list(metric.labels), name
    for line in metrics.REGISTRY.render().splitlines():                                               # the series read from the database: the labels they carry when they have rows
        found = re.match(r"^(erp_agents_\w+)\{([^}]*)\}", line)
        if found and found.group(1) in documented and found.group(1) not in metrics.REGISTRY._metrics:
            assert set(re.findall(r'(\w+)="', found.group(2))) == set(documented[found.group(1)]), found.group(1)


def test_the_numbers_the_document_gives_for_metrics_are_the_ones_in_the_code():
    assert f"after {metrics.MAX_SERIES} (counted in" in TEXT and f"held {metrics.MAX_SERIES} already" in TEXT
    assert metrics.SCRAPER_ENV in TEXT and f"{MIN_KEY_LENGTH} characters or more" in TEXT
    assert obs.LOG_FORMAT_ENV in TEXT


# ------------------------------------------------------------------ log events
def events_in_the_code():
    """{event name: the keyword fields given to it} from every obs.event(...) call, and the two <target>_request events obs.observe_session writes."""
    events, targets = {}, set()
    for path in SRC.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            is_event = (isinstance(func, ast.Attribute) and func.attr == "event" and isinstance(func.value, ast.Name) and func.value.id == "obs") or (isinstance(func, ast.Name) and func.id == "event")
            if is_event and node.args:
                first = node.args[0]
                fields = {k.arg for k in node.keywords if k.arg and k.arg != "level"}
                if isinstance(first, ast.Constant) and isinstance(first.value, str):
                    events.setdefault(first.value, set()).update(fields)
                elif isinstance(first, ast.JoinedStr):                                                 # f"{target}_request"
                    events.setdefault("<target>_request", set()).update(fields)
            if isinstance(func, ast.Attribute) and func.attr == "observe_session" and len(node.args) == 2 and isinstance(node.args[1], ast.Constant):
                targets.add(node.args[1].value)
    if "<target>_request" in events:
        shared = events.pop("<target>_request")
        for t in targets:
            events[f"{t}_request"] = set(shared)
    return events


def test_the_event_table_lists_every_event_the_code_writes_and_no_other():
    in_code = events_in_the_code()
    assert {"agent_task_start", "agent_task", "llm_call", "tool_call", "job_run", "erpnext_request", "llm_request"} == set(in_code)        # the scan itself sees what it should
    documented = {names_in(r[0])[0]: set(names_in(r[2])) for r in table("log:events")}
    assert set(documented) == set(in_code)


def test_every_field_an_event_is_given_is_in_its_row_of_the_table():
    documented = {names_in(r[0])[0]: set(names_in(r[2])) for r in table("log:events")}
    for event, fields in events_in_the_code().items():
        missing = fields - documented[event]
        assert not missing, f"{event}: {sorted(missing)} are written but not documented"


# ------------------------------------------------------------------ spans
def test_the_span_table_lists_the_spans_the_code_makes():
    in_code = set()
    for path in SRC.rglob("*.py"):
        in_code |= set(re.findall(r'tracing\.span\(\s*"([a-z.]+)"', path.read_text(encoding="utf-8")))
    assert in_code == {"agent.task", "llm.call", "tool.call", "erp.request"}
    assert {names_in(r[0])[0] for r in table("tracing:spans")} == in_code
    assert f"cut at {tracing.MAX_VALUE} characters" in TEXT


# ------------------------------------------------------------------ settings
def test_the_settings_table_matches_the_settings_and_their_defaults():
    defaults = obs.ObsSettings()
    rows = {names_in(r[0])[0]: names_in(r[1])[0] if names_in(r[1]) else "empty" for r in table("obs:settings")}
    assert set(rows) == set(obs.ObsSettings.__dataclass_fields__)
    for name, shown in rows.items():
        value = getattr(defaults, name)
        expected = ("true" if value else "false") if isinstance(value, bool) else (f"{value:g}" if isinstance(value, float) else (value or "empty"))
        assert shown == expected, name


def test_the_example_settings_file_and_env_template_describe_the_same_settings():
    example = (SRC / "settings.example.yaml").read_text(encoding="utf-8")
    assert "observability:" in example
    for name in obs.ObsSettings.__dataclass_fields__:
        assert re.search(rf"^#\s+{name}:", example, re.M), f"settings.example.yaml does not show {name}"
    env = (ROOT / ".env.example").read_text(encoding="utf-8")
    assert re.search(rf"^{metrics.SCRAPER_ENV}=$", env, re.M) and re.search(rf"^{obs.LOG_FORMAT_ENV}=$", env, re.M)


def test_the_example_observability_section_is_accepted_by_the_start_up_check():
    lines = (SRC / "settings.example.yaml").read_text(encoding="utf-8").splitlines()
    start = next(i for i, l in enumerate(lines) if l.startswith("# observability:"))
    block = []
    for l in lines[start:]:
        if not l.startswith("#"):
            break
        block.append(l[2:] if l.startswith("# ") else l[1:])
    cfg = yaml.safe_load("\n".join(block))
    settings = obs.settings_from(cfg)
    assert settings == obs.ObsSettings()                                                              # the example shows the defaults


# ------------------------------------------------------------------ thresholds
def test_the_thresholds_in_the_document_are_the_ones_in_the_code():
    assert f"within {jobs.HEARTBEAT_STALE_MINUTES} minutes" in TEXT and f"has not run for {jobs.HEARTBEAT_STALE_MINUTES} minutes" in TEXT
    assert f"below {doctor.DISK_WARN_MB} MB" in TEXT and f"below {doctor.DISK_FAIL_MB} MB" in TEXT
    assert f"more than {doctor.CLOCK_TOLERANCE_MINUTES} minutes ahead" in TEXT
    assert f"{obs.ObsSettings().pending_hours:g} hours" in TEXT


# ------------------------------------------------------------------ the alert rules
def rules():
    blocks = re.findall(r"```yaml\n(.*?)```", TEXT, re.S)
    (found,) = [b for b in blocks if "groups:" in b]
    return [r for g in yaml.safe_load(found)["groups"] for r in g["rules"]]


def test_there_are_three_alert_rules_and_each_is_complete():
    found = rules()
    assert [r["alert"] for r in found] == ["StockAssistantApprovalStuck", "StockAssistantFallbackRateHigh", "StockAssistantErpnextErrors"]
    for r in found:
        assert r["expr"].strip() and r["for"] and r["labels"]["severity"] in ("warning", "critical") and r["annotations"]["summary"] and r["annotations"]["description"], r["alert"]


def test_the_alert_rules_use_metrics_and_labels_that_exist():
    documented = {names_in(r[0])[0]: names_in(r[2]) for r in table("metrics:table")}
    for r in rules():
        used = set(re.findall(r"erp_agents_\w+", r["expr"]))
        assert used and used <= set(documented), (r["alert"], sorted(used - set(documented)))
        for name, selector in re.findall(r"(erp_agents_\w+)\{([^}]*)\}", r["expr"]):
            for label in re.findall(r"(\w+)\s*(?:=~|!~|!=|=)", selector):
                assert label in documented[name], f"{r['alert']}: {name} has no label {label}"


def test_the_stuck_approval_rule_uses_the_same_hours_as_the_setting():
    (stuck,) = [r for r in rules() if r["alert"] == "StockAssistantApprovalStuck"]
    assert f"{obs.ObsSettings().pending_hours:g} * 3600" in stuck["expr"]


# ------------------------------------------------------------------ the other documents
@pytest.mark.parametrize("path", ["README.md", "SECURITY.md", "docs/DEVELOPING.md", "docs/KNOWN_LIMITATIONS.md"])
def test_the_other_documents_point_to_it(path):
    assert "OBSERVABILITY.md" in (ROOT / path).read_text(encoding="utf-8"), path


@pytest.mark.parametrize("path", ["README.md", "SECURITY.md"])
def test_the_documents_give_the_schema_version_the_code_has(path):
    text = (ROOT / path).read_text(encoding="utf-8")
    assert f"the core is at version {SCHEMA_VERSION}" in text and "needed none yet" not in text, path
