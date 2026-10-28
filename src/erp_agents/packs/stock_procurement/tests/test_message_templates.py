"""Finding F-10 (review of 25 Oct 2026): the owner's message templates (the vendor note, the delivery reminder, the request for a price) were filled with
`str.format_map`, which also follows `{vendor.__class__}`, `{vendor[0]}`, `{vendor!r}` and `{qty:>10}`. They are now filled by a strict replacer that knows only
`{name}` for the names each template documents (and `{{` / `}}` for a literal brace), and anything else is refused when the setting is saved.
"""
import json
import logging

import pytest
from pydantic import ValidationError

from erp_agents.packs.stock_procurement.settings import CHASE_FIELDS, QUOTE_FIELDS, VENDOR_NOTE_FIELDS, FollowUpSettings, ProcurementSettings

CASES = [(FollowUpSettings, "chase_message", CHASE_FIELDS), (FollowUpSettings, "quote_message", QUOTE_FIELDS), (ProcurementSettings, "vendor_note_template", VENDOR_NOTE_FIELDS)]
NOT_A_PLAIN_PLACEHOLDER = ["{%s.__class__}", "{%s.__class__.__mro__}", "{%s[0]}", "{%s!r}", "{%s:>10}", "{%s.__len__}", "{ %s }", "{%s", "%s}", "{}", "{0}", "{nosuchfield}"]


@pytest.mark.parametrize("model,field,names", CASES, ids=[c[1] for c in CASES])
@pytest.mark.parametrize("pattern", NOT_A_PLAIN_PLACEHOLDER)
def test_a_template_that_is_not_a_plain_placeholder_is_refused_when_saved(model, field, names, pattern):
    with pytest.raises(ValidationError):
        model(**{field: "Dear " + pattern.replace("%s", names[0])})


@pytest.mark.parametrize("model,field,names", CASES, ids=[c[1] for c in CASES])
def test_every_documented_placeholder_and_a_literal_brace_are_accepted(model, field, names):
    template = " ".join("{" + n + "}" for n in names) + " {{literal}}"
    assert getattr(model(**{field: template}), field) == template


@pytest.mark.parametrize("model,field", [(FollowUpSettings, "chase_message"), (FollowUpSettings, "quote_message"), (ProcurementSettings, "vendor_note_template")])
def test_the_default_templates_are_accepted(model, field):
    assert getattr(model(), field)


def test_the_replacer_follows_nothing_but_a_name():
    from erp_agents.packs.stock_procurement.templating import TemplateError, check_template, fill_template

    values = {"vendor": "Prime Supplies", "qty": 5}
    assert fill_template("Dear {vendor}, {qty} {{x}} {vendor}", values) == "Dear Prime Supplies, 5 {x} Prime Supplies"
    for bad in ("{vendor.__class__}", "{vendor[0]}", "{vendor!r}", "{qty:>10}", "{missing}", "{vendor", "x}"):
        with pytest.raises(TemplateError):
            fill_template(bad, values)
        with pytest.raises(TemplateError):
            check_template(bad, tuple(values))


def test_a_value_is_never_looked_into_even_if_it_looks_like_a_placeholder():
    from erp_agents.packs.stock_procurement.templating import fill_template

    assert fill_template("Reference {reference}", {"reference": "{vendor.__class__}"}) == "Reference {vendor.__class__}"        # a vendor's or item's own text is just text


def test_a_template_saved_before_the_check_is_ignored_with_a_warning_instead_of_leaking_or_breaking_the_follow_up(make_app, caplog):
    app = make_app()
    app.db.execute("INSERT INTO settings_overrides(scope_type, scope_key, data_json, updated_by, updated_at) VALUES (?,?,?,?,?)",
                   ("followup", "*", json.dumps({"chase_message": "Dear {vendor.__class__} {days_late.__class__}", "chase_max": 2}), "old", "2026-10-01T00:00:00+00:00"))
    with caplog.at_level(logging.WARNING):
        cfg = app.settings.followup()
    assert cfg.chase_message == FollowUpSettings().chase_message and cfg.chase_max == 2                    # only the bad value is dropped, the rest of the save is kept
    assert any("chase_message" in r.getMessage() for r in caplog.records)
    order = {"vendor": "Prime Supplies", "document": "PO-1", "due": "2026-10-10", "days_late": 5,
             "lines": [{"item_name": "Oil", "item_code": "OIL-5L", "qty": 100.0, "still": 80.0, "uom": "Nos"}]}
    text = app.followup.chaser.message_for(order)
    assert "<class" not in text and "Prime Supplies" in text and "PO-1" in text


def test_saving_a_bad_template_through_the_settings_is_refused_and_names_the_setting(make_app):
    app = make_app()
    from conftest import OWNER

    with pytest.raises(Exception) as err:
        app.settings.update_section(OWNER, "procurement", {"vendor_note_template": "Please confirm {vendor.__class__}"})
    assert "vendor_note_template" in str(err.value)
