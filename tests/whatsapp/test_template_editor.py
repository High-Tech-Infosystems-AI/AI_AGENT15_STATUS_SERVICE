"""Template editor (docs/TEMPLATES_CONTRACT.md): categories and sub-types,
variable types, header types, buttons, LTO, carousel, authentication,
validity period, warnings, provider mapping, list filters and the API."""
import json
from datetime import datetime

import pytest
from fastapi import HTTPException

from app.dependencies.tenant_auth import AuthCtx
from app.whatsapp_layer import crypto
from app.whatsapp_layer import template_rules as R
from app.whatsapp_layer import templates_service as ts
from app.whatsapp_layer.api import templates_api
from app.whatsapp_layer.models import WhatsAppProviderAccount as A, WhatsAppTemplate as T
from app.whatsapp_layer.providers.omnichannel import OmnichannelProvider
from app.whatsapp_layer.schemas import TemplateDraftPreview, TemplateWrite

ORG = 12


def _m(**kw):
    """A valid Marketing Default template (generic purpose, numbered variables)."""
    base = {"name": "new_collection_offer", "language": "en_US", "category": "MARKETING",
            "template_type": "DEFAULT", "purpose": "generic", "variable_type": "NUMBER",
            "header_format": "TEXT", "header_text": "New Arrival {{1}}",
            "header_sample_values": {"1": "Shoes"},
            "body_text": "Hi {{1}}, your order {{2}} is ready.", "footer_text": "Reply STOP",
            "buttons": [], "sample_values": {"1": "Abhishek", "2": "4679"}}
    base.update(kw)
    return base


def _fields(errors):
    return {e["field"] for e in errors}


def _msgs(errors):
    return [e["message"] for e in errors]


# ---------------------------------------------------------------------------
# Name, category, sub-type
# ---------------------------------------------------------------------------
def test_valid_marketing_template():
    assert ts.validate_template(_m()) == []
    assert ts.validate_template(_m(), for_submit=True) == []


@pytest.mark.parametrize("name", ["1abc", "_abc", "Abc", "ab-c", "a b", "x" * 513, ""])
def test_name_rule_exact_message(name):
    errs = ts.validate_template(_m(name=name))
    assert {"field": "name", "message": R.NAME_ERROR} in errs
    assert R.NAME_ERROR == ("Template name must start with a lowercase letter, contain only "
                            "[a-z0-9_] and be max 512 chars.")


def test_name_max_512_ok():
    assert "name" not in _fields(ts.validate_template(_m(name="a" * 512)))


@pytest.mark.parametrize("category,tt,ok", [
    ("MARKETING", "DEFAULT", True), ("MARKETING", "CATALOGUE", True),
    ("MARKETING", "CALLING_PERMISSION_REQUEST", True), ("MARKETING", "OTP", False),
    ("UTILITY", "DEFAULT", True), ("UTILITY", "CALLING_PERMISSION_REQUEST", True),
    ("UTILITY", "CATALOGUE", False),
])
def test_sub_types_per_category(category, tt, ok):
    errs = ts.validate_template(_m(category=category, template_type=tt))
    assert ("template_type" not in _fields(errs)) is ok


def test_sub_type_defaults():
    assert ts.normalise({"category": "marketing"})["template_type"] == "DEFAULT"
    assert ts.normalise({"category": "AUTHENTICATION"})["template_type"] == "OTP"
    assert ts.normalise({"category": "UTILITY", "template_type": "calling_permission"})[
        "template_type"] == "CALLING_PERMISSION_REQUEST"


# ---------------------------------------------------------------------------
# Variables
# ---------------------------------------------------------------------------
def test_number_mode_rejects_named_placeholders():
    errs = ts.validate_template(_m(body_text="Hi {{name}}, ok."))
    assert any("Only numeric variables" in m for m in _msgs(errs))


def test_name_mode_rejects_numbered_placeholders():
    errs = ts.validate_template(_m(variable_type="NAME", header_text="Hi {{1}}",
                                   body_text="Hi {{customer_name}} and {{2}} ok."))
    assert {"header_text", "body_text"} <= {e["field"] for e in errs
                                           if "Only named variables" in e["message"]}


def test_name_mode_allows_free_names_for_generic():
    data = _m(variable_type="NAME", header_text="For {{first_name}}",
              body_text="Hi {{customer_name}}, order {{order_id}} shipped.",
              header_sample_values={"first_name": "A"},
              sample_values={"customer_name": "John", "order_id": "99"})
    assert ts.validate_template(data, for_submit=True) == []


def test_numbered_variables_must_be_sequential():
    errs = ts.validate_template(_m(body_text="Hi {{1}}, see {{3}} now."))
    assert any("without gaps" in m for m in _msgs(errs))


def test_header_number_variable_must_be_one():
    errs = ts.validate_template(_m(header_text="Hi {{2}}"))
    assert any("must be {{1}}" in m for m in _msgs(errs))


def test_every_variable_needs_a_sample_on_submit():
    data = _m(header_sample_values={}, sample_values={"1": "A"})
    assert ts.validate_template(data) == []
    errs = ts.validate_template(data, for_submit=True)
    # header {{1}} falls back to the body's {{1}} sample; {{2}} has none.
    assert errs == [{"field": "sample_values", "message": "Sample value required for '{{2}}'"}]
    errs = ts.validate_template(_m(header_sample_values={}, sample_values={}), for_submit=True)
    assert "header_sample_values" in _fields(errs)


def test_variable_type_inferred_when_omitted():
    assert ts.normalise({"body_text": "Hi {{1}} x"})["variable_type"] == "NUMBER"
    assert ts.normalise({"body_text": "Hi {{a}} x"})["variable_type"] == "NAME"


def test_clawback_purposes_need_named_variables():
    errs = ts.validate_template(_m(purpose="recruiter_reminder", category="UTILITY",
                                   body_text="Hi {{1}} there"))
    assert "variable_type" in _fields(errs)


# ---------------------------------------------------------------------------
# Header types and limits
# ---------------------------------------------------------------------------
def test_limits_header_body_footer():
    assert "header_text" in _fields(ts.validate_template(_m(header_text="h" * 61)))
    assert "body_text" in _fields(ts.validate_template(_m(body_text="b" * 1025,
                                                          sample_values={})))
    assert "footer_text" in _fields(ts.validate_template(_m(footer_text="f" * 61)))
    assert ts.validate_template(_m(header_text="h" * 60, body_text="b" * 1024,
                                   footer_text="f" * 60, header_sample_values={},
                                   sample_values={})) == []


@pytest.mark.parametrize("fmt", ["IMAGE", "VIDEO", "DOCUMENT"])
def test_media_header_requires_media_on_submit(fmt):
    data = _m(header_format=fmt)
    assert ts.validate_template(data) == []
    assert "header_media" in _fields(ts.validate_template(data, for_submit=True))
    ok = _m(header_format=fmt, header_media={"url": "https://cdn.hti.test/f"})
    assert ts.validate_template(ok, for_submit=True) == []
    bad = _m(header_format="IMAGE", header_media={"s3_key": "k", "mime_type": "application/pdf"})
    assert "header_media.mime_type" in _fields(ts.validate_template(bad))


def test_non_text_header_drops_header_text():
    d = ts.normalise(_m(header_format="IMAGE"))
    assert d["header_text"] is None and d["header_sample_values"] == {}
    assert ts.validate_template(_m(header_format="LOCATION"), for_submit=True) == []
    assert "header_format" in _fields(ts.validate_template(_m(header_format="GIF")))


# ---------------------------------------------------------------------------
# Buttons
# ---------------------------------------------------------------------------
PHONE = {"type": "PHONE_NUMBER", "label": "Call Now", "phone_number": "+911234567890"}
URL = {"type": "URL", "label": "Shop now", "url": "https://shop.hti.test/item"}
VOICE = {"type": "VOICE_CALL", "label": "Call support"}
COPY = {"type": "COPY_CODE", "label": "Copy Code", "example": "SAVE420"}
MPM = {"type": "MPM", "label": "View products"}
FLOW = {"type": "FLOW", "label": "Start", "flow_id": "2200", "flow_action": "navigate",
        "navigate_screen": "WELCOME"}
QR = {"type": "QUICK_REPLY", "label": "buy"}


def test_all_button_types_in_marketing_default():
    buttons = [PHONE, URL, dict(URL, label="Two"), VOICE, COPY, MPM, FLOW, QR, QR]
    assert ts.validate_template(_m(buttons=buttons), for_submit=True) == []


@pytest.mark.parametrize("button,cap", [(PHONE, 1), (URL, 2), (VOICE, 1), (COPY, 1),
                                        (MPM, 1), (FLOW, 1), (QR, 5)])
def test_per_type_caps(button, cap):
    assert ts.validate_template(_m(buttons=[button] * cap)) == []
    errs = ts.validate_template(_m(buttons=[button] * (cap + 1)))
    assert any(e["field"] == "buttons" and e["message"].startswith(f"At most {cap} ")
               for e in errs)


def test_only_one_website_button_in_utility():
    assert ts.validate_template(_m(category="UTILITY", buttons=[URL])) == []
    errs = ts.validate_template(_m(category="UTILITY", buttons=[URL, URL]))
    assert "At most 1 Url button in Utility templates" in _msgs(errs)


@pytest.mark.parametrize("button,category,tt,ok", [
    (MPM, "MARKETING", "CATALOGUE", False), (MPM, "UTILITY", "DEFAULT", False),
    (FLOW, "MARKETING", "CALLING_PERMISSION_REQUEST", False), (FLOW, "UTILITY", "DEFAULT", False),
    (COPY, "UTILITY", "DEFAULT", False), (COPY, "MARKETING", "CATALOGUE", True),
    (PHONE, "UTILITY", "CALLING_PERMISSION_REQUEST", True), (VOICE, "UTILITY", "DEFAULT", True),
    (QR, "UTILITY", "DEFAULT", True),
])
def test_button_availability(button, category, tt, ok):
    errs = ts.validate_template(_m(category=category, template_type=tt, buttons=[button]))
    assert ("buttons[0].type" not in _fields(errs)) is ok


def test_button_field_rules():
    errs = ts.validate_template(_m(buttons=[
        {"type": "PHONE_NUMBER", "label": "Call", "phone_number": "12"},
        {"type": "COPY_CODE", "label": "Copy", "example": "x" * 16},
        {"type": "FLOW", "label": "Go", "flow_action": "navigate"},
        {"type": "URL", "label": "L" * 26, "url": "ftp://x"},
    ]))
    assert {"buttons[0].phone_number", "buttons[1].example", "buttons[2].flow_id",
            "buttons[2].navigate_screen", "buttons[3].label", "buttons[3].url"} <= _fields(errs)


def test_button_text_alias_and_copy_code_default_label():
    b = R.normalise_button({"type": "quick_reply", "text": "Yes"})
    assert b == {"type": "QUICK_REPLY", "label": "Yes"}
    assert R.normalise_button({"type": "COPY_CODE", "example": "X"})["label"] == "Copy offer code"


def test_website_url_one_trailing_variable():
    one = dict(URL, url="https://shop.hti.test/o/{{1}}")
    assert ts.validate_template(_m(buttons=[one])) == []
    errs = ts.validate_template(_m(buttons=[one]), for_submit=True)
    assert "buttons[0].url_example" in _fields(errs)
    assert ts.validate_template(_m(buttons=[dict(one, url_example="abc")]), for_submit=True) == []
    mid = dict(URL, url="https://shop.hti.test/{{1}}/x")
    assert "The URL variable must be at the end of the URL" in _msgs(ts.validate_template(_m(buttons=[mid])))
    two = dict(URL, url="https://shop.hti.test/{{1}}{{2}}")
    assert "A URL may contain only one variable" in _msgs(ts.validate_template(_m(buttons=[two])))


def test_placeholder_url_is_a_warning_not_an_error():
    data = _m(buttons=[dict(URL, url="https://example.com/x")])
    assert ts.validate_template(data, for_submit=True) == []
    assert ts.template_warnings(data) == [{
        "field": "buttons[0].url",
        "message": "Warning: Meta may reject templates with placeholder URLs (e.g., example.com). "
                   "Please use a real, valid URL."}]
    assert ts.template_warnings(_m(buttons=[URL])) == []


# ---------------------------------------------------------------------------
# LTO, carousel, authentication, validity
# ---------------------------------------------------------------------------
def test_limited_time_offer():
    lto = {"text": "Expire soon", "expires_at": "2026-09-30", "has_expiration": True}
    assert ts.validate_template(_m(limited_time_offer=lto)) == []
    errs = ts.validate_template(_m(category="UTILITY", limited_time_offer=lto))
    assert "limited_time_offer" in _fields(errs)
    errs = ts.validate_template(_m(limited_time_offer={"text": "x" * 17, "expires_at": "soon"}))
    assert {"limited_time_offer.text", "limited_time_offer.expires_at"} <= _fields(errs)
    assert ts.normalise(_m(limited_time_offer={"enabled": False, "text": "x"}))["limited_time_offer"] is None


def _card(**kw):
    c = {"media": {"url": "https://cdn.hti.test/1.png", "media_type": "IMAGE"},
         "body_text": "Card {{1}} body", "sample_values": {"1": "A"},
         "buttons": [{"type": "URL", "label": "Check it here!", "url": "https://shop.hti.test"},
                     {"type": "QUICK_REPLY", "label": "More"}]}
    c.update(kw)
    return c


def test_carousel_cards():
    data = _m(header_format="CAROUSEL", carousel_cards=[_card(), _card()])
    assert ts.validate_template(data, for_submit=True) == []
    assert "carousel_cards" in _fields(ts.validate_template(_m(header_format="CAROUSEL")))
    assert "carousel_cards" in _fields(ts.validate_template(_m(header_format="CAROUSEL",
                                                               carousel_cards=[_card()] * 11)))
    bad = _card(media={"url": "https://x.test/" + "a" * 1600}, body_text="b" * 1025,
                buttons=[{"type": "URL", "label": "x" * 26, "url": "https://a.test/" + "u" * 2000},
                         {"type": "QUICK_REPLY", "label": "a"}, {"type": "QUICK_REPLY", "label": "b"}])
    errs = ts.validate_template(_m(header_format="CAROUSEL", carousel_cards=[bad]))
    assert {"carousel_cards[0].media.url", "carousel_cards[0].body_text",
            "carousel_cards[0].buttons", "carousel_cards[0].buttons[0].label",
            "carousel_cards[0].buttons[0].url"} <= _fields(errs)
    no_media = _card(media={"s3_key": None})
    assert "carousel_cards[0].media" in _fields(
        ts.validate_template(_m(header_format="CAROUSEL", carousel_cards=[no_media]), for_submit=True))
    # Cards are dropped when the header is not a carousel.
    assert ts.normalise(_m(carousel_cards=[_card()]))["carousel_cards"] is None


def _auth(**cfg):
    base = {"code_delivery": "ONE_TAP",
            "apps": [{"package_name": "com.hti.whatsapp", "signature_hash": "K8a/AlNcGX7"}],
            "add_security_recommendation": True, "code_expiration_minutes": 6}
    base.update(cfg)
    return {"name": "login_otp", "language": "en_US", "category": "AUTHENTICATION",
            "purpose": "generic", "auth_config": base,
            "header_format": "TEXT", "header_text": "ignored", "buttons": [QR]}


def test_authentication_template():
    assert ts.validate_template(_auth(), for_submit=True) == []
    d = ts.normalise(_auth())
    assert d["template_type"] == "OTP" and d["header_format"] == "NONE" and d["buttons"] == []
    assert d["body_text"] == "{{1}} is your verification code. For your security, do not share this code."
    assert ts.validate_template(_auth(code_delivery="COPY_CODE", apps=[]), for_submit=True) == []
    errs = ts.validate_template(_auth(code_delivery="ZERO_TAP", apps=[]))
    assert "auth_config.apps" in _fields(errs)
    errs = ts.validate_template(_auth(apps=[{"package_name": "p" * 225, "signature_hash": "short"},
                                            {"package_name": "nodots", "signature_hash": "K8a/AlNcGX7"}],
                                      code_expiration_minutes=91, code_delivery="SMS"))
    assert {"auth_config.apps[0].package_name", "auth_config.apps[0].signature_hash",
            "auth_config.apps[1].package_name", "auth_config.code_expiration_minutes",
            "auth_config.code_delivery"} <= _fields(errs)
    many = [{"package_name": f"com.a.b{i}", "signature_hash": "K8a/AlNcGX7"} for i in range(6)]
    assert "auth_config.apps" in _fields(ts.validate_template(_auth(apps=many)))


def test_validity_period_utility_only():
    data = _m(category="UTILITY", custom_validity_period=True, validity_period_seconds=600)
    assert ts.validate_template(data) == []
    errs = ts.validate_template(_m(category="UTILITY", custom_validity_period=True,
                                   validity_period_seconds=5))
    assert "validity_period_seconds" in _fields(errs)
    errs = ts.validate_template(_m(custom_validity_period=True, validity_period_seconds=600))
    assert "custom_validity_period" in _fields(errs)
    assert ts.normalise(_m(validity_period_seconds=600))["validity_period_seconds"] is None


# ---------------------------------------------------------------------------
# Preview
# ---------------------------------------------------------------------------
def test_preview_number_mode_uses_header_samples():
    out = ts.preview_of(_m(buttons=[dict(URL, url="https://shop.hti.test/{{1}}", url_example="o9")],
                           limited_time_offer={"text": "Expire soon", "has_expiration": True}))
    assert out["header"] == "New Arrival Shoes"
    assert out["body"] == "Hi Abhishek, your order 4679 is ready."
    assert out["buttons"][0]["url"] == "https://shop.hti.test/o9"
    assert out["limited_time_offer"]["text"] == "Expire soon"
    out = ts.preview_of(_m(sample_values={"1": "A"}))
    assert out["body"] == "Hi A, your order {{2}} is ready."


def test_preview_auth_and_carousel():
    out = ts.preview_of(_auth())
    assert out["body"] == "*123456* is your verification code. For your security, do not share this code."
    assert out["footer"] == "Expires in 6 minutes."
    assert out["buttons"] == [{"type": "OTP", "label": "Copy Code", "otp_type": "ONE_TAP"}]
    out = ts.preview_of(_m(header_format="CAROUSEL", carousel_cards=[_card()]))
    assert out["carousel_cards"][0]["body"] == "Card A body"
    assert out["carousel_cards"][0]["media"]["preview_url"] == "https://cdn.hti.test/1.png"


# ---------------------------------------------------------------------------
# Provider mapping
# ---------------------------------------------------------------------------
def _fields_of(spec):
    parts = OmnichannelProvider.build_template_fields(spec)
    return parts, {k: v[1] for k, v in parts}


def _spec_for(data, header_file=None):
    tpl = T(organization_id=ORG, version=1, is_active=1, approval_status="draft")
    ts.apply_fields(tpl, data)
    return ts.build_spec(tpl, header_file)


def test_provider_fields_marketing_media_and_buttons():
    data = _m(header_format="IMAGE", header_media={"s3_key": "k", "file_name": "a.png",
                                                    "mime_type": "image/png"},
              buttons=[PHONE, dict(URL, url="https://shop.hti.test/{{1}}", url_example="o9"), VOICE,
                       COPY, MPM, FLOW, QR],
              limited_time_offer={"text": "Expire soon", "has_expiration": True})
    spec = _spec_for(data, ("a.png", b"PNG", "image/png"))
    parts, f = _fields_of(spec)
    assert f["category"] == "MARKETING" and f["templateType"] == "DEFAULT"
    assert f["subCategory"] == "DEFAULT" and f["headerFormat"] == "IMAGE"
    assert "headerText" not in f
    assert dict(parts)["headerfile"] == ("a.png", b"PNG", "image/png")
    assert f["bodyText"] == "Hi {{1}}, your order {{2}} is ready."
    assert f["bodyExample"] == '[["Abhishek", "4679"]]'
    assert [v[1] for k, v in parts if k == "buttonRequest"] == [
        "PHONE_NUMBER:Call Now|+911234567890", "URL:Shop now|https://shop.hti.test/{{1}}|o9",
        "VOICE_CALL:Call support", "COPY_CODE:Copy Code|SAVE420", "MPM:View products",
        "FLOW:Start|2200|navigate|WELCOME", "QUICK_REPLY:buy"]
    assert json.loads(f["limitedTimeOffer"]) == {"text": "Expire soon", "hasExpiration": True}


def test_provider_fields_named_variables_become_positional():
    data = _m(variable_type="NAME", header_text="For {{first_name}}",
              header_sample_values={"first_name": "Ravi"},
              body_text="Hi {{customer_name}}, order {{order_id}} ok.",
              sample_values={"customer_name": "John", "order_id": "9"},
              buttons=[dict(URL, url="https://shop.hti.test/{{order_id}}", url_example="9")])
    _, f = _fields_of(_spec_for(data))
    assert f["headerText"] == "For {{1}}" and f["headerExample"] == '["Ravi"]'
    assert f["bodyText"] == "Hi {{1}}, order {{2}} ok." and f["bodyExample"] == '[["John", "9"]]'
    assert f["buttonRequest"] == "URL:Shop now|https://shop.hti.test/{{1}}|9"


@pytest.mark.parametrize("category,tt,expected", [
    ("MARKETING", "CATALOGUE", "catalogue"),
    ("MARKETING", "CALLING_PERMISSION_REQUEST", "CALLING_PERMISSION"),
    ("UTILITY", "DEFAULT", "DEFAULT"),
    ("UTILITY", "CALLING_PERMISSION_REQUEST", "CALLING_PERMISSION"),
])
def test_provider_template_type(category, tt, expected):
    _, f = _fields_of(_spec_for(_m(category=category, template_type=tt)))
    assert f["templateType"] == expected and f["category"] == category


def test_provider_utility_validity_period_in_minutes():
    _, f = _fields_of(_spec_for(_m(category="UTILITY", custom_validity_period=True,
                                   validity_period_seconds=90)))
    assert f["customValidityPeriod"] == "true" and f["timeToLive"] == "2"
    _, f = _fields_of(_spec_for(_m(category="UTILITY")))
    assert "customValidityPeriod" not in f


def test_provider_auth_fields():
    _, f = _fields_of(_spec_for(_auth(code_delivery="ZERO_TAP")))
    assert f["category"] == "AUTHENTICATION" and f["templateType"] == "OTP"
    assert f["subCategory"] == "ONE_TIME_PASSCODE" and f["headerFormat"] == "NONE"
    assert f["customValidityPeriod"] == "true" and f["timeToLive"] == "6"
    assert "bodyText" not in f and "buttonRequest" not in f
    auth = json.loads(f["authConfig"])
    assert auth == {"otpType": "ZERO_TAP", "addSecurityRecommendation": True,
                    "supportedApps": [{"packageName": "com.hti.whatsapp",
                                       "signatureHash": "K8a/AlNcGX7"}],
                    "codeExpirationMinutes": 6, "zeroTapTermsAccepted": True}


def test_provider_carousel_fields():
    _, f = _fields_of(_spec_for(_m(header_format="CAROUSEL", carousel_cards=[_card()])))
    assert f["headerFormat"] == "NONE"
    cards = json.loads(f["carouselCards"])
    assert cards == [{"components": [
        {"type": "HEADER", "format": "IMAGE", "example": {"header_url": ["https://cdn.hti.test/1.png"]}},
        {"type": "BODY", "text": "Card {{1}} body", "example": {"body_text": [["A"]]}},
        {"type": "BUTTONS", "buttons": [
            {"type": "URL", "text": "Check it here!", "url": "https://shop.hti.test"},
            {"type": "QUICK_REPLY", "text": "More"}]}]}]


def test_parse_template_reads_type_header_buttons():
    t = OmnichannelProvider.parse_template({
        "name": "x", "templateType": "UTILITY_DEFAULT", "timeToLive": 30,
        "headerContentJson": json.dumps({"type": "IM AGE", "filename": "a.jpg", "mediaId": "9"}),
        "buttonsJson": json.dumps([{"text": "OK", "type": "QUICK_REPLY"},
                                   {"label": "View", "type": "URL", "url": "https://a.test"}])})
    assert t.template_type == "DEFAULT" and t.time_to_live == 30
    assert t.header_format == "IMAGE" and t.header_media == {"file_name": "a.jpg", "provider_media_id": "9"}
    assert t.buttons == [{"type": "QUICK_REPLY", "label": "OK"},
                         {"type": "URL", "label": "View", "url": "https://a.test"}]
    assert OmnichannelProvider.parse_template({"templateType": "CALLING_PERMISSION"}).template_type == \
        "CALLING_PERMISSION_REQUEST"


# ---------------------------------------------------------------------------
# Model round trip + serialisation
# ---------------------------------------------------------------------------
def test_serialize_list_columns(wa_db):
    tpl = T(organization_id=ORG, version=1, is_active=1, approval_status="pending",
            previous_category="MARKETING")
    ts.apply_fields(tpl, _m(category="UTILITY", buttons=[dict(URL, url="https://example.com/a"), QR],
                            custom_validity_period=True, validity_period_seconds=600))
    wa_db.add(tpl)
    wa_db.commit()
    out = ts.serialize(tpl)
    assert out["status"] == "PENDING" and out["language_label"] == "English (US)"
    assert out["template_type"] == "DEFAULT" and out["template_type_label"] == "UTILITY DEFAULT"
    assert out["previous_category"] == "MARKETING" and out["header_type"] == "TEXT"
    assert out["time_to_live"] == 600 and out["variable_type"] == "NUMBER"
    assert out["button_types"] == ["URL", "QUICK_REPLY"]
    assert out["button_summary"] == ["Url: https://example.com/a", "Quick Reply: buy"]
    assert out["warnings"] and out["warnings"][0]["field"] == "buttons[0].url"
    assert out["variables"] == ["1", "2"]
    auth = T(organization_id=ORG, version=1, is_active=1, approval_status="draft")
    ts.apply_fields(auth, _auth())
    out = ts.serialize(auth)
    assert out["template_type_label"] == "OTP" and out["button_summary"] == ["otp: Copy Code"]
    assert out["header_type"] == "NONE" and out["time_to_live"] == 0


def test_send_variables_header_override():
    vm = ts.build_variable_map("TEXT", "Hi {{1}}", "A {{1}} and {{2}} b", "NUMBER")
    assert vm == {"header": ["1"], "body": ["1", "2"]}
    assert ts.send_variables(vm, {"1": "x", "2": "y", "header_1": "H"}) == (["H"], ["x", "y"])
    assert ts.send_variables(vm, {"1": "x", "2": "y"}) == (["x"], ["x", "y"])


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------
def _ctx():
    return AuthCtx(user_id=1, organization_id=ORG, role_id=1, role_name="admin", role_type="admin",
                   permissions=set(), is_super_admin=False, is_admin=True, token="t")


def _acct(db):
    a = A(organization_id=ORG, provider="omnichannel", display_name="One",
          base_url="https://one.broadnet.test", access_token_enc=crypto.encrypt("tok"),
          token_last4="tok", webhook_token="wh-one-" + "x" * 24, status="active")
    db.add(a)
    db.commit()
    return a


def _list(db, **kw):
    args = dict(purpose=None, approval_status=None, search=None, page=1, limit=100,
                organization_id=None, account_id=None, ctx=_ctx(), db=db)
    args.update(kw)
    return [t["name"] for t in templates_api.list_templates(**args)["data"]]


def test_api_create_update_get_and_warnings(wa_db):
    a = _acct(wa_db)
    body = TemplateWrite(**_m(buttons=[dict(URL, url="https://example.com/x")]))
    out = templates_api.create_template(body, None, None, _ctx(), wa_db)
    assert out["account_id"] == a.id and out["template_type"] == "DEFAULT"
    assert out["header_sample_values"] == {"1": "Shoes"}
    assert out["warnings"][0]["message"].startswith("Warning: Meta may reject")
    with pytest.raises(HTTPException) as err:
        templates_api.create_template(TemplateWrite(**_m(name="Bad Name")), None, None, _ctx(), wa_db)
    assert err.value.status_code == 422 and {"field": "name", "message": R.NAME_ERROR} in err.value.detail
    upd = templates_api.update_template(out["id"], TemplateWrite(**_m(category="UTILITY", buttons=[QR])),
                                        _ctx(), wa_db)
    assert upd["category"] == "UTILITY" and upd["warnings"] == []
    got = templates_api.get_template(out["id"], _ctx(), wa_db)
    assert got["buttons"] == [{"type": "QUICK_REPLY", "label": "buy"}]


def test_api_auth_template_without_body(wa_db):
    _acct(wa_db)
    a = _auth()
    a.pop("buttons")
    out = templates_api.create_template(TemplateWrite(**a), None, None, _ctx(), wa_db)
    assert out["category"] == "AUTHENTICATION" and out["template_type"] == "OTP"
    assert out["auth_config"]["apps"][0]["signature_hash"] == "K8a/AlNcGX7"


def test_api_list_filters(wa_db):
    _acct(wa_db)
    for kw in (dict(name="promo_one"), dict(name="util_one", category="UTILITY", language="hi"),
               dict(name="promo_cat", template_type="CATALOGUE")):
        templates_api.create_template(TemplateWrite(**_m(**kw)), None, None, _ctx(), wa_db)
    templates_api.create_template(TemplateWrite(**{k: v for k, v in _auth().items() if k != "buttons"}),
                                  None, None, _ctx(), wa_db)
    wa_db.query(T).filter(T.name == "promo_one").update({"approval_status": "approved",
                                                          "created_at": datetime(2026, 3, 5)})
    wa_db.query(T).filter(T.name != "promo_one").update({"created_at": datetime(2026, 3, 20)})
    wa_db.commit()
    assert _list(wa_db) == ["login_otp", "promo_cat", "promo_one", "util_one"]
    assert _list(wa_db, status="APPROVED") == ["promo_one"]
    assert _list(wa_db, approval_status="draft") == ["login_otp", "promo_cat", "util_one"]
    assert _list(wa_db, name="promo") == ["promo_cat", "promo_one"]
    assert _list(wa_db, language="hi") == ["util_one"]
    assert _list(wa_db, category="marketing") == ["promo_cat", "promo_one"]
    assert _list(wa_db, template_type="CATALOGUE") == ["promo_cat"]
    assert _list(wa_db, template_type="UTILITY DEFAULT") == ["util_one"]
    assert _list(wa_db, template_type="OTP") == ["login_otp"]
    assert _list(wa_db, template_type="DEFAULT") == ["promo_one", "util_one"]
    assert _list(wa_db, from_date="2026-03-10T00:00:00") == ["login_otp", "promo_cat", "util_one"]
    assert _list(wa_db, to_date="10-03-2026 00:00:00.000") == ["promo_one"]
    assert _list(wa_db, from_date="02-03-2026 05:30:00.000", to_date="2026-03-06") == ["promo_one"]
    with pytest.raises(HTTPException) as err:
        _list(wa_db, from_date="yesterday")
    assert err.value.status_code == 422


def test_api_validate_preview_options(wa_db):
    out = templates_api.validate_template(TemplateWrite(**_m(sample_values={})), True, _ctx())
    assert out["valid"] is False and {"field": "sample_values",
                                      "message": "Sample value required for '{{1}}'"} in out["errors"]
    out = templates_api.validate_template(TemplateWrite(**_m(buttons=[dict(URL, url="https://example.com")])),
                                          False, _ctx())
    assert out["valid"] is True and len(out["warnings"]) == 1
    prev = templates_api.preview_draft(TemplateDraftPreview(**_m(), values={"2": "777"}), _ctx())
    assert prev["body"] == "Hi Abhishek, your order 777 is ready." and prev["header"] == "New Arrival Shoes"
    opts = templates_api.template_options(_ctx())
    assert opts["template_types"]["UTILITY"] == ["DEFAULT", "CALLING_PERMISSION_REQUEST"]
    caps = {b["type"]: (b["max"], b["max_in_utility"]) for b in opts["button_types"]}
    assert caps == {"PHONE_NUMBER": (1, 1), "URL": (2, 1), "VOICE_CALL": (1, 1), "COPY_CODE": (1, 1),
                    "MPM": (1, 1), "FLOW": (1, 1), "QUICK_REPLY": (5, 5)}
    assert opts["limits"]["body"] == 1024 and opts["name_error"] == R.NAME_ERROR


def test_api_saved_preview(wa_db):
    _acct(wa_db)
    out = templates_api.create_template(TemplateWrite(**_m()), None, None, _ctx(), wa_db)
    prev = templates_api.preview_template(out["id"], None, _ctx(), wa_db)
    assert prev["header_type"] == "TEXT" and prev["body"] == "Hi Abhishek, your order 4679 is ready."


def test_api_submit_media_template(wa_db, monkeypatch):
    _acct(wa_db)
    out = templates_api.create_template(
        TemplateWrite(**_m(header_format="IMAGE", header_media={"url": "https://cdn.hti.test/a.png"})),
        None, None, _ctx(), wa_db)
    monkeypatch.setattr(ts, "fetch_media", lambda media, kind: ("a.png", b"PNG", "image/png"))
    sent = []
    from app.whatsapp_layer.providers import registry
    provider = registry.get_provider("omnichannel")
    from app.whatsapp_layer.providers.base import ProviderTemplate
    monkeypatch.setattr(type(provider), "create_template",
                        lambda self, creds, spec: sent.append(spec) or ProviderTemplate(
                            name=spec.name, language=spec.language, provider_template_id="p1",
                            status="pending"))
    res = templates_api.submit_template(out["id"], _ctx(), wa_db)
    assert res["status"] == "PENDING" and res["provider_template_id"] == "p1"
    assert sent[0].header_file == ("a.png", b"PNG", "image/png")
    assert sent[0].header_format == "IMAGE"


def test_api_submit_media_error_is_422(wa_db, monkeypatch):
    _acct(wa_db)
    out = templates_api.create_template(
        TemplateWrite(**_m(name="m2", header_format="VIDEO",
                           header_media={"url": "https://cdn.hti.test/a.mp4"})),
        None, None, _ctx(), wa_db)

    def boom(media, kind):
        raise ts.MediaError("Could not download the media (HTTP 404)")
    monkeypatch.setattr(ts, "fetch_media", boom)
    with pytest.raises(HTTPException) as err:
        templates_api.submit_template(out["id"], _ctx(), wa_db)
    assert err.value.status_code == 422
    assert err.value.detail == [{"field": "header_media", "message": "Could not download the media (HTTP 404)"}]


def test_api_submit_requires_media_and_samples(wa_db):
    _acct(wa_db)
    out = templates_api.create_template(TemplateWrite(**_m(name="m3", header_format="DOCUMENT",
                                                           sample_values={})),
                                        None, None, _ctx(), wa_db)
    with pytest.raises(HTTPException) as err:
        templates_api.submit_template(out["id"], _ctx(), wa_db)
    assert err.value.status_code == 422
    assert {"header_media", "sample_values"} <= {e["field"] for e in err.value.detail}


def test_api_delete_any_status(wa_db):
    _acct(wa_db)
    out = templates_api.create_template(TemplateWrite(**_m(name="gone")), None, None, _ctx(), wa_db)
    wa_db.query(T).filter(T.id == out["id"]).update({"approval_status": "approved"})
    wa_db.commit()
    assert templates_api.delete_template(out["id"], _ctx(), wa_db).status_code == 204
    assert _list(wa_db) == []


def test_fetch_media_blocks_private_hosts():
    with pytest.raises(ts.MediaError):
        ts.fetch_media({"url": "http://127.0.0.1/x.png"}, "IMAGE")
    with pytest.raises(ts.MediaError):
        ts.fetch_media({"url": "file:///etc/passwd"}, "IMAGE")


def test_media_kind():
    assert ts.media_kind("application/octet-stream", "a.PDF") == ("DOCUMENT", "application/pdf")
    assert ts.media_kind("image/png", "x") == ("IMAGE", "image/png")
    assert ts.media_kind("audio/mpeg", "a.mp3")[0] is None
    with pytest.raises(ts.MediaError):
        ts.upload_media(ORG, b"x", "image/png", "a.png", media_type="VIDEO")
    with pytest.raises(ts.MediaError):
        ts.upload_media(ORG, b"x" * (5 * 1024 * 1024 + 1), "image/png", "a.png")


# ---------------------------------------------------------------------------
# HTTP routing (query aliases, route order, multipart upload)
# ---------------------------------------------------------------------------
@pytest.fixture
def http(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from sqlalchemy import create_engine, event
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from app.database_Layer.db_config import Base, get_db
    from app.dependencies.tenant_auth import get_auth_ctx
    from app.whatsapp_layer import models as m
    engine = create_engine("sqlite://", future=True, poolclass=StaticPool,
                           connect_args={"check_same_thread": False})

    @event.listens_for(engine, "connect")
    def _connect(dbapi_conn, _rec):
        dbapi_conn.isolation_level = None

    @event.listens_for(engine, "begin")
    def _begin(conn):
        conn.exec_driver_sql("BEGIN")

    Base.metadata.create_all(engine, tables=[mdl.__table__ for mdl in (
        m.WhatsAppProviderAccount, m.WhatsAppTemplate, m.ClawbackNotificationRule,
        m.ClawbackDigestRule, m.ChannelDefault)])
    db = sessionmaker(bind=engine, expire_on_commit=False, future=True)()
    app = FastAPI()
    app.include_router(templates_api.router, prefix="/status/whatsapp")
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_auth_ctx] = _ctx
    try:
        yield db, TestClient(app)
    finally:
        db.close()
        engine.dispose()


def test_http_routes(http, monkeypatch):
    db, client = http
    _acct(db)
    base = "/status/whatsapp/templates"
    r = client.post(base, json=_m(buttons=[{"type": "quick_reply", "text": "Yes"}]))
    assert r.status_code == 200, r.text
    assert r.json()["buttons"] == [{"type": "QUICK_REPLY", "label": "Yes"}]
    assert client.get(f"{base}/options").json()["limits"]["footer"] == 60
    r = client.get(base, params={"from": "2000-01-01T00:00:00", "to": "2999-01-01",
                                 "status": "DRAFT", "category": "MARKETING", "name": "collection"})
    assert [t["name"] for t in r.json()["data"]] == ["new_collection_offer"]
    assert client.get(base, params={"to": "2000-01-01"}).json()["data"] == []
    r = client.post(f"{base}/validate?for_submit=true", json=_m(sample_values={}))
    assert r.json()["valid"] is False
    r = client.post(f"{base}/preview", json={**_m(), "values": {"1": "Zed"}})
    assert r.json()["body"] == "Hi Zed, your order 4679 is ready."
    r = client.post(base, json=_m(name="Bad Name"))
    assert r.status_code == 422 and r.json()["detail"][0] == {"field": "name", "message": R.NAME_ERROR}
    # Upload: wrong type → 422, S3 not configured → 503, configured → stored object.
    r = client.post(f"{base}/media", files={"file": ("a.mp3", b"x", "audio/mpeg")})
    assert r.status_code == 422 and r.json()["detail"][0]["field"] == "file"
    r = client.post(f"{base}/media", files={"file": ("a.png", b"PNG", "image/png")})
    assert r.status_code == 503
    from app.chat_layer import s3_chat_service as s3
    put = []
    monkeypatch.setattr(s3, "_is_configured", lambda: True)
    monkeypatch.setattr(s3, "_client", lambda: type("C", (), {"put_object": lambda self, **kw: put.append(kw)})())
    monkeypatch.setattr(s3, "presign_get", lambda key: f"https://signed.test/{key}")
    r = client.post(f"{base}/media", files={"file": ("a.png", b"PNG", "image/png")},
                    data={"media_type": "IMAGE"})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["media_type"] == "IMAGE" and out["mime_type"] == "image/png" and out["size_bytes"] == 3
    assert out["s3_key"].startswith(f"whatsapp_templates/{ORG}/") and out["preview_url"].startswith("https://signed.test/")
    assert put[0]["Key"] == out["s3_key"] and put[0]["ContentType"] == "image/png"
