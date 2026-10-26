"""Named ↔ positional conversion, contract §2.2 validation, preview, send vars."""
import pytest

from app.whatsapp_layer import seeds
from app.whatsapp_layer import templates_service as ts
from app.whatsapp_layer import variables as V


def _tpl(**kw):
    base = {"name": "clawback_check", "language": "en_US", "category": "UTILITY",
            "purpose": "recruiter_reminder", "header_format": "NONE", "header_text": None,
            "body_text": "Hi {{recruiter_name}}, check {{candidate_name}} today.",
            "footer_text": "HTI HRMIS", "buttons": [],
            "sample_values": {"recruiter_name": "Asha", "candidate_name": "Ravi"}}
    base.update(kw)
    return base


def _fields(errors):
    return {e["field"] for e in errors}


# ---------------------------------------------------------------------------
# Conversion
# ---------------------------------------------------------------------------
def test_named_to_positional():
    text, keys = ts.to_positional("Hi {{recruiter_name}}, {{candidate_name}} is on day {{day_number}}.")
    assert text == "Hi {{1}}, {{2}} is on day {{3}}."
    assert keys == ["recruiter_name", "candidate_name", "day_number"]


def test_repeated_key_keeps_position_and_spaces_tolerated():
    text, keys = ts.to_positional("{{ a }} and {{b}} and {{a}}")
    assert text == "{{1}} and {{2}} and {{1}}" and keys == ["a", "b"]


def test_positional_to_named_roundtrip():
    named = "Hi {{recruiter_name}}, {{candidate_name}} ({{job_title}})."
    pos, keys = ts.to_positional(named)
    assert ts.to_named(pos, keys) == named
    assert ts.to_named("x {{5}} y", ["a"]) == "x {{5}} y"


def test_variable_map_and_send_variables():
    vm = ts.build_variable_map("TEXT", "For {{candidate_name}}", "Hi {{recruiter_name}}, {{day_number}} days.")
    assert vm == {"header": ["candidate_name"], "body": ["recruiter_name", "day_number"]}
    header, body = ts.send_variables(vm, {"candidate_name": "Ravi\nK", "recruiter_name": "Asha",
                                          "day_number": 14})
    assert header == ["Ravi K"] and body == ["Asha", "14"]
    assert ts.send_variables(vm, {})[1] == ["-", "-"]


def test_clean_param():
    assert ts.clean_param("a\n\tb") == "a b"
    assert ts.clean_param("a      b") == "a   b"
    assert ts.clean_param(None) == "-" and ts.clean_param("") == "-"


# ---------------------------------------------------------------------------
# Validation (contract §2.2)
# ---------------------------------------------------------------------------
def test_valid_template_passes_draft_and_submit():
    assert ts.validate_template(_tpl()) == []
    assert ts.validate_template(_tpl(), for_submit=True) == []


@pytest.mark.parametrize("name", ["Bad-Name", "has space", "", "UPPER", "x" * 513])
def test_name_format(name):
    assert "name" in _fields(ts.validate_template(_tpl(name=name)))


def test_body_must_not_start_or_end_with_variable():
    errs = ts.validate_template(_tpl(body_text="{{recruiter_name}} hello {{candidate_name}}"))
    msgs = [e["message"] for e in errs]
    assert "Body must not start with a variable" in msgs
    assert "Body must not end with a variable" in msgs


def test_adjacent_variables_rejected():
    errs = ts.validate_template(_tpl(body_text="Hi {{recruiter_name}}{{candidate_name}} there"))
    assert any("next to each other" in e["message"] for e in errs)
    errs = ts.validate_template(_tpl(body_text="Hi {{recruiter_name}} {{candidate_name}} there"))
    assert any("next to each other" in e["message"] for e in errs)


def test_unknown_and_disallowed_variables():
    errs = ts.validate_template(_tpl(body_text="Hi {{nope}} there"))
    assert any("Unknown variable" in e["message"] for e in errs)
    errs = ts.validate_template(_tpl(body_text="Hi {{due_count}} there"))  # batch-only key
    assert any("not allowed for purpose" in e["message"] for e in errs)
    errs = ts.validate_template(_tpl(body_text="Hi {{1}} there"))  # raw positional
    assert any("Unknown variable" in e["message"] for e in errs)


def test_body_length_after_expansion():
    # "Hi {{1}} " is 9 chars once expanded, so 1014 x's + "." is exactly 1024
    # (the named form is 1037 chars and must still pass).
    ok_body = "Hi {{recruiter_name}} " + "x" * 1014 + "."
    assert len(ok_body) > 1024 and ts.validate_template(_tpl(body_text=ok_body)) == []
    long_body = "Hi {{recruiter_name}} " + "x" * 1015 + "."
    assert "body_text" in _fields(ts.validate_template(_tpl(body_text=long_body)))
    assert "body_text" in _fields(ts.validate_template(_tpl(body_text="   ")))


def test_header_rules():
    errs = ts.validate_template(_tpl(header_format="TEXT", header_text=None))
    assert "header_text" in _fields(errs)
    errs = ts.validate_template(_tpl(header_format="TEXT",
                                     header_text="{{candidate_name}} {{job_title}}"))
    assert any("at most 1 variable" in e["message"] for e in errs)
    errs = ts.validate_template(_tpl(header_format="TEXT", header_text="y" * 61))
    assert any("at most 60" in e["message"] for e in errs)
    assert ts.validate_template(_tpl(header_format="TEXT", header_text="For {{candidate_name}}",
                                     sample_values={"recruiter_name": "A", "candidate_name": "R"}),
                                for_submit=True) == []


def test_footer_rules():
    assert "footer_text" in _fields(ts.validate_template(_tpl(footer_text="z" * 61)))
    assert "footer_text" in _fields(ts.validate_template(_tpl(footer_text="Hi {{recruiter_name}}")))


def test_buttons_rules():
    four = [{"type": "QUICK_REPLY", "label": f"b{i}"} for i in range(4)]
    assert "buttons" in _fields(ts.validate_template(_tpl(buttons=four)))
    errs = ts.validate_template(_tpl(buttons=[{"type": "URL", "label": "Open"}]))
    assert "buttons[0].url" in _fields(errs)
    errs = ts.validate_template(_tpl(buttons=[{"type": "CALL", "label": "x"}]))
    assert "buttons[0].type" in _fields(errs)
    assert ts.validate_template(_tpl(buttons=[{"type": "URL", "label": "Open",
                                               "url": "https://hrmis.example.com"},
                                              {"type": "QUICK_REPLY", "label": "Done"}])) == []


def test_sample_values_required_only_for_submit():
    tpl = _tpl(sample_values={"recruiter_name": "Asha"})
    assert ts.validate_template(tpl) == []
    errs = ts.validate_template(tpl, for_submit=True)
    assert errs == [{"field": "sample_values", "message": "Sample value required for '{{candidate_name}}'"}]


def test_enums():
    errs = ts.validate_template(_tpl(category="PROMO", purpose="x", header_format="IMAGE", language="english"))
    assert {"category", "purpose", "header_format", "language"} <= _fields(errs)


@pytest.mark.parametrize("spec", seeds.DEFAULT_TEMPLATES, ids=lambda s: s["name"])
def test_seed_templates_are_submittable(spec):
    data = _tpl(name=spec["name"], purpose=spec["purpose"], body_text=spec["body_text"],
                footer_text=None, sample_values=seeds._sample_values(spec["body_text"]))
    assert ts.validate_template(data, for_submit=True) == []


def test_variable_catalogue_matches_contract():
    assert set(V.allowed_keys("recruiter_batch")) == {"recruiter_name", "due_count", "overdue_count", "app_link"}
    assert set(V.allowed_keys("team_digest")) == {
        "recipient_name", "scope_label", "period_label", "active_count", "due_count", "done_count",
        "missed_count", "at_risk_count", "ending_soon_count", "final_pending_count", "app_link"}
    assert len(V.allowed_keys("recruiter_reminder")) == 13
    assert set(V.allowed_keys("generic")) == set(V.VARIABLES_BY_KEY)


def test_app_links():
    base = "https://hrmis.example.com/"
    assert V.app_link(base, "recruiter_reminder", health_check_id=301) == \
        "https://hrmis.example.com/clawback/health-checks?check=301"
    assert V.app_link(base, "recruiter_batch") == "https://hrmis.example.com/clawback/health-checks"
    assert V.app_link(base, "final_validation", job_id=9) == \
        "https://hrmis.example.com/job-pipeline/9?tab=clawback"
    assert V.app_link(base, "team_digest") == "https://hrmis.example.com/clawback/team-health"


# ---------------------------------------------------------------------------
# Preview
# ---------------------------------------------------------------------------
def test_preview_falls_back_to_sample_values():
    out = ts.render_preview("TEXT", "For {{candidate_name}}", "Hi {{recruiter_name}}, see {{candidate_name}}.",
                            "Footer", [{"type": "URL", "label": "Open", "url": "https://x/{{candidate_name}}"}],
                            values={"recruiter_name": "Meera"},
                            sample_values={"recruiter_name": "Asha", "candidate_name": "Ravi"})
    assert out["header"] == "For Ravi"
    assert out["body"] == "Hi Meera, see Ravi."
    assert out["footer"] == "Footer"
    assert out["buttons"][0]["url"] == "https://x/Ravi"
    out = ts.render_preview("NONE", "ignored", "Hi {{recruiter_name}}.", None, None)
    assert out["header"] is None and out["body"] == "Hi {{recruiter_name}}." and out["buttons"] == []


def test_version_names():
    assert ts.base_name_of("clawback_health_check_due_v3") == "clawback_health_check_due"
    assert ts.base_name_of("plain") == "plain"
    assert ts.next_version_name("plain", 2) == "plain_v2"
