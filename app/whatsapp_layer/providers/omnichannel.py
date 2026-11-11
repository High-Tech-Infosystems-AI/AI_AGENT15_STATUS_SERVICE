"""Omnichannel (BroadNet CPaaS / "Advance Messaging Systems") WhatsApp API.

Documented and used here (vendor doc v1.0.0):
  - POST {base}/whatsapp/message/sendbulk      multipart, messageType=TEMPLATE
  - POST {base}/whatsapp/template              multipart, create template
  - GET  {base}/whatsapp/message/edit?batch_Id= batch status
  - GET  {base}/whatsapp/clients-onboard/me    account health
Auth is `Authorization: Bearer <User-Access-Token>` everywhere.

NOT documented (index lists §3.24–3.27 but the body pages are missing), so
delete template is a best-effort guess marked `# UNCONFIRMED`. The webhook
body is Meta's own, forwarded unchanged (CONFIRMED on staging for message
statuses, template status updates and Flow alerts; see parse_webhook).

The batch-status endpoint (§2.34) answers HTTP 400 "... current status is
DONE ..." for a finished batch on the live server; get_send_status reads
that as sent.

Correlation: one send = one BroadNet batch (batchId = provider_ref), and we
set campaignName = our correlation id ("cbk_<log_id>").
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, List, Optional, Tuple

import requests

from app.whatsapp_layer.providers.base import (
    AccountHealth, ProviderCredentials, ProviderError, ProviderTemplate,
    ProviderUnsupported, SendResult, SendStatus, TemplateRef, TemplateSpec,
    WebhookEvent, WhatsAppProvider,
)

logger = logging.getLogger("app_logger")

# Provider template status → our approval_status vocabulary.
_TEMPLATE_STATUS_MAP = {
    "APPROVED": "approved",
    "PENDING": "pending",
    "IN_APPEAL": "pending",
    "PENDING_DELETION": "disabled",
    "REJECTED": "rejected",
    "PAUSED": "paused",
    "DISABLED": "disabled",
    "DELETED": "disabled",
    "FLAGGED": "approved",   # still sendable; quality warning only
    "REINSTATED": "approved",  # back from PAUSED
    "IN_REVIEW": "pending",
    "LIMIT_EXCEEDED": "paused",
}


# 400 text the live batch-status endpoint returns for a finished batch.
_FINISHED_BATCH_RE = re.compile(
    r"current\s+status\s+is\s+(DONE|FAILED|ABORTED|CANCELLED|CANCELED)\b", re.IGNORECASE)

LIST_PAGE_SIZE = 50
LIST_MAX_PAGES = 100


def normalise_template_status(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    return _TEMPLATE_STATUS_MAP.get(str(value).strip().upper())


def _upper(value: Any) -> Optional[str]:
    return str(value).strip().upper() if value not in (None, "") else None


def _json_field(value: Any) -> Any:
    """BroadNet returns some template fields as JSON inside a string."""
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return None
    return value


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _error_text(resp: requests.Response) -> str:
    try:
        body = resp.json()
        if isinstance(body, dict):
            for k in ("message", "error", "errorMessage", "detail"):
                if body.get(k):
                    return str(body[k])[:500]
        return json.dumps(body)[:500]
    except ValueError:
        return (resp.text or "")[:500]


class OmnichannelProvider(WhatsAppProvider):
    key = "omnichannel"
    label = "Omnichannel"

    def __init__(self, http: Optional[requests.Session] = None,
                 timeout: float = 15.0):
        # `http` is injectable so tests can mock the HTTP layer.
        self.http = http or requests.Session()
        self.timeout = timeout

    # ------------------------------------------------------------------
    # HTTP helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _url(creds: ProviderCredentials, path: str) -> str:
        return f"{(creds.base_url or '').rstrip('/')}/{path.lstrip('/')}"

    @staticmethod
    def _headers(creds: ProviderCredentials) -> Dict[str, str]:
        return {"Authorization": f"Bearer {creds.access_token}",
                "Accept": "application/json"}

    def _request(self, method: str, creds: ProviderCredentials, path: str,
                 **kwargs) -> requests.Response:
        """Send a request; network errors / timeouts become retryable
        ProviderErrors. HTTP status is left to the caller."""
        try:
            return self.http.request(method, self._url(creds, path),
                                     headers=self._headers(creds),
                                     timeout=self.timeout, **kwargs)
        except requests.Timeout as exc:
            raise ProviderError(f"Provider timeout: {exc}", retryable=True) from exc
        except requests.RequestException as exc:
            raise ProviderError(f"Provider unreachable: {exc}", retryable=True) from exc

    @staticmethod
    def _raise_for_status(resp: requests.Response) -> None:
        code = resp.status_code
        if code < 400:
            return
        msg = _error_text(resp)
        if code in (401, 403):
            raise ProviderError(f"Provider rejected the access token ({code}): {msg}",
                                status_code=code, auth_error=True)
        if code == 404:
            raise ProviderUnsupported(f"Not found (404): {msg}", status_code=code)
        raise ProviderError(f"Provider HTTP {code}: {msg}", status_code=code,
                            retryable=(code >= 500 or code == 429))

    @staticmethod
    def _json(resp: requests.Response) -> Any:
        try:
            return resp.json()
        except ValueError:
            return None

    # ------------------------------------------------------------------
    # Send
    # ------------------------------------------------------------------
    @staticmethod
    def build_send_fields(to_digits: str, template: TemplateRef,
                          header_vars: List[str], body_vars: List[str],
                          correlation_id: str) -> List[Tuple[str, Tuple[None, str]]]:
        """multipart/form-data fields for sendbulk (exact vendor names).

        Returned as a list of (name, (None, value)) so `requests` sends a
        multipart body without file parts and keeps field order."""
        fields: List[Tuple[str, str]] = [
            ("messageType", "TEMPLATE"),
            ("destinationNo", to_digits),
            ("templateName", template.name),
            ("templateLanguage", template.language),
            ("batchId", "0"),
            ("campaignName", correlation_id),
            # Recruiters legitimately get repeat messages; without this
            # BroadNet blocks the second send to the same number.
            ("allowDuplicate", "true"),
        ]
        if header_vars:
            fields.append(("headerTemplateVariables", json.dumps(list(header_vars))))
        # Named templates take {"parameter_name", "text"} per variable (as the
        # BroadNet portal sends them); a bare value is a parameter mismatch.
        names = template.body_param_names
        for i, value in enumerate(body_vars or []):
            if i < len(names):
                value = json.dumps({"parameter_name": names[i], "text": str(value)},
                                   ensure_ascii=False, separators=(",", ":"))
            fields.append((f"bodyTemplateVariables[{i}]", str(value)))
        return [(k, (None, v)) for k, v in fields]

    @staticmethod
    def parse_send_response(status_code: int, body: Any,
                            text: str = "") -> SendResult:
        """Map a sendbulk HTTP response to a SendResult (pure; unit-tested)."""
        if status_code in (401, 403):
            return SendResult(accepted=False, error_code=str(status_code),
                              error_message="Provider rejected the access token",
                              auth_error=True, raw=body)
        if status_code >= 500 or status_code == 429:
            return SendResult(accepted=False, error_code=str(status_code),
                              error_message=f"Provider HTTP {status_code}: {text[:300]}",
                              retryable=True, raw=body)
        if status_code >= 400:
            msg = None
            if isinstance(body, dict):
                msg = body.get("message") or body.get("error")
            return SendResult(accepted=False, error_code=str(status_code),
                              error_message=str(msg or text or f"HTTP {status_code}")[:500],
                              raw=body)
        if not isinstance(body, dict):
            return SendResult(accepted=False, error_code="bad_response",
                              error_message="Provider returned a non-JSON response",
                              retryable=True, raw=text[:500])
        batch_id = body.get("batchId")
        batch_id = str(batch_id) if batch_id not in (None, "", 0, "0") else None
        if _int(body.get("totalNumbersDuplicate")) > 0:
            return SendResult(accepted=False, duplicate=True, provider_ref=batch_id,
                              error_code="duplicate",
                              error_message="Provider flagged the number as duplicate",
                              raw=body)
        if _int(body.get("totalNumbersSuccess")) > 0:
            return SendResult(accepted=True, provider_ref=batch_id,
                              provider_message_id=body.get("messageId") or body.get("wamid"),
                              raw=body)
        if _int(body.get("totalNumbersFailed")) > 0:
            return SendResult(accepted=False, provider_ref=batch_id,
                              error_code="provider_failed",
                              error_message="Provider reported the number as failed",
                              raw=body)
        return SendResult(accepted=False, provider_ref=batch_id,
                          error_code="bad_response",
                          error_message="Provider response had no success/failed count",
                          raw=body)

    def send_template(self, creds, to_digits, template, header_vars, body_vars,
                      correlation_id) -> SendResult:
        fields = self.build_send_fields(to_digits, template, header_vars,
                                        body_vars, correlation_id)
        try:
            resp = self._request("POST", creds, "/whatsapp/message/sendbulk", files=fields)
        except ProviderError as exc:
            return SendResult(accepted=False, error_code="network",
                              error_message=exc.message, retryable=exc.retryable)
        return self.parse_send_response(resp.status_code, self._json(resp), resp.text or "")

    def send_text(self, creds, to_digits, text, correlation_id) -> SendResult:
        fields = [(k, (None, v)) for k, v in (
            ("messageType", "TEXT"), ("destinationNo", to_digits), ("content", text),
            ("batchId", "0"), ("campaignName", correlation_id), ("allowDuplicate", "true"))]
        try:
            resp = self._request("POST", creds, "/whatsapp/message/sendbulk", files=fields)
        except ProviderError as exc:
            return SendResult(accepted=False, error_code="network",
                              error_message=exc.message, retryable=exc.retryable)
        return self.parse_send_response(resp.status_code, self._json(resp), resp.text or "")

    # ------------------------------------------------------------------
    # Templates
    # ------------------------------------------------------------------
    # Our sub-type → vendor `templateType` (doc §3: DEFAULT, catalogue,
    # CALLING_PERMISSION, OTP). Utility Default is sent as DEFAULT with
    # subCategory DEFAULT; the vendor reports it back as UTILITY_DEFAULT.
    _TEMPLATE_TYPE_OUT = {"DEFAULT": "DEFAULT", "CATALOGUE": "catalogue",
                          "CALLING_PERMISSION_REQUEST": "CALLING_PERMISSION", "OTP": "OTP"}
    _TEMPLATE_TYPE_IN = {"DEFAULT": "DEFAULT", "UTILITY_DEFAULT": "DEFAULT",
                         "CATALOGUE": "CATALOGUE", "CALLING_PERMISSION": "CALLING_PERMISSION_REQUEST",
                         "CALLING_PERMISSION_REQUEST": "CALLING_PERMISSION_REQUEST", "OTP": "OTP"}

    @staticmethod
    def button_request(b: Dict[str, Any]) -> Optional[str]:
        """One `buttonRequest` value, "TYPE:Label|value" (doc §3.1)."""
        btype = (b.get("type") or "").upper()
        label = b.get("label") or ""
        if btype == "URL":
            value = f"URL:{label}|{b.get('url') or ''}"
            if b.get("example"):
                # UNCONFIRMED: sample for a URL ending in {{1}} as a third segment.
                value += f"|{b['example']}"
            return value
        if btype == "QUICK_REPLY":
            return f"QUICK_REPLY:{label}"
        if btype == "PHONE_NUMBER":
            return f"PHONE_NUMBER:{label}|{b.get('phone_number') or ''}"
        # UNCONFIRMED: the doc only shows URL / QUICK_REPLY / PHONE_NUMBER; the
        # rest follow the same TYPE:Label|value pattern with Meta's type names.
        if btype == "VOICE_CALL":
            return f"VOICE_CALL:{label}"
        if btype == "COPY_CODE":
            return f"COPY_CODE:{label}|{b.get('example') or ''}"
        if btype == "MPM":
            return f"MPM:{label}"
        if btype == "FLOW":
            return (f"FLOW:{label}|{b.get('flow_id') or ''}|{b.get('flow_action') or 'navigate'}"
                    f"|{b.get('navigate_screen') or ''}")
        return None

    @staticmethod
    def _meta_button(b: Dict[str, Any]) -> Dict[str, Any]:
        """Meta Cloud API button object (used inside UNCONFIRMED JSON fields)."""
        btype = (b.get("type") or "").upper()
        out: Dict[str, Any] = {"type": btype, "text": b.get("label") or ""}
        if btype == "URL":
            out["url"] = b.get("url") or ""
            if b.get("example"):
                out["example"] = [b["example"]]
        elif btype == "PHONE_NUMBER":
            out["phone_number"] = b.get("phone_number") or ""
        return out

    @classmethod
    def build_template_fields(cls, spec: TemplateSpec) -> List[Tuple[str, Tuple]]:
        category = (spec.category or "UTILITY").upper()
        template_type = (spec.template_type or "DEFAULT").upper()
        header_format = (spec.header_format or "NONE").upper()
        is_auth = category == "AUTHENTICATION"
        if header_format == "CAROUSEL":
            # UNCONFIRMED: carousel is not a vendor header format; cards go in
            # `carouselCards` (below) and the header is NONE, as in Meta.
            sent_header = "NONE"
        else:
            # LOCATION is UNCONFIRMED (doc lists TEXT, IMAGE, DOCUMENT, VIDEO, NONE).
            sent_header = header_format
        fields: List[Tuple[str, Any]] = [
            ("name", spec.name),
            ("languageType", spec.language),
            ("category", category),
            ("templateType", cls._TEMPLATE_TYPE_OUT.get(template_type, "DEFAULT")),
            ("subCategory", "ONE_TIME_PASSCODE" if is_auth else "DEFAULT"),
            ("headerFormat", "NONE" if is_auth else sent_header),
        ]
        if not is_auth:
            fields.append(("bodyText", spec.body_text))
        if header_format == "TEXT" and spec.header_text:
            fields.append(("headerText", spec.header_text))
            if spec.header_example:
                fields.append(("headerExample", json.dumps(list(spec.header_example))))
        if spec.body_example and not is_auth:
            fields.append(("bodyExample", json.dumps([list(spec.body_example)])))
        if spec.footer_text and not is_auth:
            fields.append(("footerText", spec.footer_text))
        # The doc shows one `buttonRequest` string; several buttons repeat the field.
        for b in spec.buttons or []:
            value = cls.button_request(b)
            if value:
                fields.append(("buttonRequest", value))
        if spec.custom_validity_period and spec.time_to_live:
            fields.append(("customValidityPeriod", "true"))
            fields.append(("timeToLive", str(spec.time_to_live)))
        if spec.limited_time_offer:
            # UNCONFIRMED: not in the vendor doc; Meta's limited_time_offer component.
            lto = spec.limited_time_offer
            fields.append(("limitedTimeOffer", json.dumps(
                {"text": lto.get("text") or "", "hasExpiration": bool(lto.get("has_expiration"))})))
        if header_format == "CAROUSEL" and spec.carousel_cards:
            # UNCONFIRMED: not in the vendor doc; Meta's carousel card components.
            cards = []
            for c in spec.carousel_cards:
                comps: List[Dict[str, Any]] = [{
                    "type": "HEADER", "format": c.get("media_type") or "IMAGE",
                    "example": {"header_url": [c.get("media_url") or ""]}}]
                if c.get("body_text"):
                    body: Dict[str, Any] = {"type": "BODY", "text": c["body_text"]}
                    if c.get("body_example"):
                        body["example"] = {"body_text": [list(c["body_example"])]}
                    comps.append(body)
                if c.get("buttons"):
                    comps.append({"type": "BUTTONS",
                                  "buttons": [cls._meta_button(b) for b in c["buttons"]]})
                cards.append({"components": comps})
            fields.append(("carouselCards", json.dumps(cards, ensure_ascii=False)))
        if is_auth and spec.auth_config:
            # UNCONFIRMED: §3.21–3.23 bodies are missing from the vendor doc. Field
            # name from the `authConfigJson` response key; values follow Meta.
            cfg = spec.auth_config
            auth = {"otpType": cfg.get("code_delivery") or "COPY_CODE",
                    "addSecurityRecommendation": bool(cfg.get("add_security_recommendation")),
                    "supportedApps": [{"packageName": a.get("package_name"),
                                       "signatureHash": a.get("signature_hash")}
                                      for a in cfg.get("apps") or []]}
            if cfg.get("code_expiration_minutes"):
                auth["codeExpirationMinutes"] = int(cfg["code_expiration_minutes"])
            if cfg.get("button_text"):
                auth["buttonText"] = cfg["button_text"]
            if cfg.get("autofill_text"):
                auth["autofillText"] = cfg["autofill_text"]
            if auth["otpType"] == "ZERO_TAP":
                auth["zeroTapTermsAccepted"] = True
            fields.append(("authConfig", json.dumps(auth)))
        parts: List[Tuple[str, Tuple]] = [(k, (None, v)) for k, v in fields]
        if header_format in ("IMAGE", "VIDEO", "DOCUMENT") and spec.header_file and not is_auth:
            parts.append(("headerfile", tuple(spec.header_file)))
        return parts

    @classmethod
    def _parse_buttons(cls, raw: Any) -> Optional[List[Dict[str, Any]]]:
        items = _json_field(raw)
        if not isinstance(items, list):
            return None
        out = []
        for i in items:
            if not isinstance(i, dict):
                continue
            btype = str(i.get("type") or "").upper()
            if btype in ("OTP",):
                continue
            b = {"type": btype, "label": i.get("label") or i.get("text") or ""}
            for src, dst in (("url", "url"), ("phone_number", "phone_number"),
                             ("phoneNumber", "phone_number")):
                if i.get(src):
                    b[dst] = i[src]
            out.append(b)
        return out

    @classmethod
    def parse_template(cls, body: Any) -> ProviderTemplate:
        body = body if isinstance(body, dict) else {}
        reason = body.get("rejectionReason")
        if not reason and body.get("rejectionInfoJson"):
            reason = str(body.get("rejectionInfoJson"))[:500]
        pid = body.get("id")
        header = _json_field(body.get("headerContentJson"))
        header = header if isinstance(header, dict) else {}
        examples = _json_field(body.get("bodyExampleJson"))
        samples = {e["param_name"]: str(e.get("example") or "")
                   for e in (examples if isinstance(examples, list) else [])
                   if isinstance(e, dict) and e.get("param_name")}
        header_type = str(header.get("type") or "").replace(" ", "").upper() or None
        media = None
        if header_type in ("IMAGE", "VIDEO", "DOCUMENT"):
            media = {k: v for k, v in (("file_name", header.get("filename")),
                                       ("provider_media_id", header.get("mediaId")))
                     if v}
        ttl = body.get("timeToLive")
        return ProviderTemplate(
            name=body.get("name"),
            language=body.get("language") or body.get("languageType"),
            provider_template_id=str(pid) if pid not in (None, "") else None,
            status=normalise_template_status(body.get("status")),
            category=body.get("category"),
            previous_category=body.get("previousCategory"),
            rejection_reason=reason,
            header_text=header.get("text"),
            body_text=body.get("bodyText"),
            footer_text=body.get("footerText"),
            sample_values=samples,
            parameter_format=body.get("parameterFormat"),
            template_type=cls._TEMPLATE_TYPE_IN.get(str(body.get("templateType") or "").upper()),
            header_format=header_type,
            header_media=media or None,
            buttons=cls._parse_buttons(body.get("buttonsJson")),
            time_to_live=_int(ttl) if ttl not in (None, "") else None,
            raw=body,
        )

    def create_template(self, creds, spec) -> ProviderTemplate:
        resp = self._request("POST", creds, "/whatsapp/template",
                             files=self.build_template_fields(spec))
        self._raise_for_status(resp)
        return self.parse_template(self._json(resp))

    # Read endpoints are missing from the vendor doc (§3.24–3.27); confirmed
    # against the live API. A bare 404 / 405 surfaces as ProviderUnsupported
    # so the sync task can treat "no status API" as a no-op.
    def get_template(self, creds, name, language) -> Optional[ProviderTemplate]:
        resp = self._request("GET", creds, "/whatsapp/template", params={"name": name})
        if resp.status_code == 404 and self._error_code(resp) == "NOT_FOUND":
            return None
        if resp.status_code in (404, 405, 501):
            raise ProviderUnsupported("Template read API not available",
                                      status_code=resp.status_code)
        self._raise_for_status(resp)
        body = self._json(resp)
        candidates = body if isinstance(body, list) else (
            body.get("data") or body.get("content") or [body]
            if isinstance(body, dict) else [])
        for item in candidates or []:
            tpl = self.parse_template(item)
            if tpl.name in (None, name) and (not tpl.language or tpl.language == language):
                return tpl
        return None

    def _error_code(self, resp) -> Optional[str]:
        body = self._json(resp)
        return body.get("errorCode") if isinstance(body, dict) else None

    def list_templates(self, creds, category=None) -> List[ProviderTemplate]:
        """Every page of GET /whatsapp/template/list (`page` is 0-based)."""
        out: List[ProviderTemplate] = []
        for page in range(LIST_MAX_PAGES):
            params = {"page": page, "size": LIST_PAGE_SIZE}
            if category:
                params["category"] = category
            resp = self._request("GET", creds, "/whatsapp/template/list", params=params)
            if resp.status_code in (404, 405, 501):
                raise ProviderUnsupported("Template list API not available",
                                          status_code=resp.status_code)
            self._raise_for_status(resp)
            body = self._json(resp)
            items = body if isinstance(body, list) else (
                (body.get("data") or body.get("content") or []) if isinstance(body, dict) else [])
            out.extend(self.parse_template(i) for i in items if isinstance(i, dict))
            if not items or not isinstance(body, dict) or body.get("lastPage", True):
                break
        return out

    def delete_template(self, creds, name) -> None:
        # UNCONFIRMED: vendor doc §3.24–3.27 missing
        resp = self._request("DELETE", creds, f"/whatsapp/template/{name}")
        if resp.status_code in (404, 405, 501):
            raise ProviderUnsupported("Template delete API not available",
                                      status_code=resp.status_code)
        self._raise_for_status(resp)
    # --- end UNCONFIRMED ------------------------------------------------

    # ------------------------------------------------------------------
    # Batch status
    # ------------------------------------------------------------------
    @staticmethod
    def parse_batch_status(body: Any) -> SendStatus:
        if isinstance(body, dict) and isinstance(body.get("content"), list) and body["content"]:
            body = body["content"][0]
        if isinstance(body, list) and body:
            body = body[0]
        if not isinstance(body, dict):
            return SendStatus(status="unknown", raw=body)
        status = str(body.get("status") or "").strip().lower()
        processed = _int(body.get("processedCount"))
        pending = _int(body.get("pendingCount"))
        if status in ("done", "completed", "complete"):
            return SendStatus(status="sent" if processed >= 1 else "failed",
                              processed=processed, pending=pending,
                              error_message=None if processed >= 1 else "Batch finished with nothing processed",
                              raw=body)
        if status in ("failed", "error", "cancelled", "canceled"):
            return SendStatus(status="failed", processed=processed, pending=pending,
                              error_message=f"Batch status {body.get('status')}", raw=body)
        return SendStatus(status="pending", processed=processed, pending=pending, raw=body)

    @staticmethod
    def finished_batch_status(status_code: int, text: str) -> Optional[SendStatus]:
        """The live server answers HTTP 400 for a finished batch instead of
        the documented Done body (vendor doc §2.34), e.g.
          "Cannot edit batch 2611111136064561001 — current status is DONE
           (allowed: PENDING / INPROGRESS / PAUSED)."
        DONE → sent (processed count unknown; delivery comes from the
        webhook); FAILED / ABORTED / CANCELLED → failed. Anything else → None."""
        if status_code != 400:
            return None
        m = _FINISHED_BATCH_RE.search(text or "")
        if not m:
            return None
        word = m.group(1).upper()
        if word == "DONE":
            return SendStatus(status="sent", raw=(text or "")[:500])
        return SendStatus(status="failed", error_message=f"Batch status {word}",
                          raw=(text or "")[:500])

    def get_send_status(self, creds, provider_ref) -> SendStatus:
        resp = self._request("GET", creds, "/whatsapp/message/edit",
                             params={"batch_Id": provider_ref})
        finished = self.finished_batch_status(
            resp.status_code, f"{_error_text(resp)} {resp.text or ''}")
        if finished is not None:
            return finished
        self._raise_for_status(resp)
        return self.parse_batch_status(self._json(resp))

    # ------------------------------------------------------------------
    # Account health
    # ------------------------------------------------------------------
    @staticmethod
    def parse_health(body: Any) -> AccountHealth:
        body = body if isinstance(body, dict) else {}
        status = body.get("status")
        sandbox = body.get("sandbox")
        return AccountHealth(
            ok=str(status or "").upper() in ("ACTIVE", "CONNECTED", ""),
            status=status,
            quality_rating=body.get("qualityRating"),
            messaging_limit=body.get("currentLimit"),
            sandbox=bool(sandbox) if sandbox is not None else None,
            display_phone_number=body.get("displayPhoneNumber"),
            waba_id=body.get("wabaId"),
            phone_number_id=body.get("phoneNumberId"),
            raw={k: v for k, v in body.items() if k not in ("accessToken",)},
        )

    def health_check(self, creds) -> AccountHealth:
        try:
            resp = self._request("GET", creds, "/whatsapp/clients-onboard/me")
            self._raise_for_status(resp)
        except ProviderError as exc:
            return AccountHealth(ok=False, auth_error=exc.auth_error, error=exc.message)
        return self.parse_health(self._json(resp))

    # ------------------------------------------------------------------
    # Webhook
    # ------------------------------------------------------------------
    def parse_webhook(self, headers, body) -> List[WebhookEvent]:
        # BroadNet forwards Meta's WhatsApp Business webhook body unchanged
        # (CONFIRMED on staging for the fields marked below):
        #   field "messages" (CONFIRMED) → value.statuses[] {id (wamid), status,
        #       timestamp, recipient_id, pricing, errors?, biz_opaque_callback_data
        #       = '{"msg_id":..,"username":"..","batch_id":..}'} and value.messages[]
        #   field "message_template_status_update" (CONFIRMED) → value {event:
        #       APPROVED|REJECTED|PENDING|PAUSED|DISABLED|FLAGGED|REINSTATED|
        #       PENDING_DELETION|DELETED|..., reason, message_template_id,
        #       message_template_name, message_template_language,
        #       message_template_category, other_info?}
        #   field "flows" (CONFIRMED) → value {event: ENDPOINT_AVAILABILITY|...,
        #       flow_id, message, threshold, alert_state, availability}
        #   field "message_template_quality_update" → value {previous_quality_score,
        #       new_quality_score, message_template_id, message_template_name,
        #       message_template_language}
        #   field "template_category_update" → value {message_template_id,
        #       message_template_name, message_template_language,
        #       previous_category, new_category | correct_category}
        # The last two follow Meta's documented shape; not yet seen from BroadNet.
        # Flat {"statuses":[...]} / top-level batchId|campaignName are still read.
        events: List[WebhookEvent] = []
        if not isinstance(body, (dict, list)):
            return events
        payloads = body if isinstance(body, list) else [body]
        for payload in payloads:
            if not isinstance(payload, dict):
                continue
            top_batch = payload.get("batchId") or payload.get("batch_id")
            top_campaign = payload.get("campaignName") or payload.get("campaign_name")
            values: List[Tuple[Optional[str], Dict[str, Any]]] = []
            for entry in payload.get("entry") or []:
                for change in (entry or {}).get("changes") or []:
                    values.append(((change or {}).get("field"), (change or {}).get("value") or {}))
            if not values:
                values.append((payload.get("field"), payload.get("value") or payload))

            for field_name, value in values:
                if not isinstance(value, dict):
                    continue
                tpl_keys = dict(
                    template_name=value.get("message_template_name"),
                    template_language=value.get("message_template_language"),
                    provider_template_id=(str(value["message_template_id"])
                                          if value.get("message_template_id") is not None else None),
                    raw=value)
                if field_name == "message_template_quality_update" or (
                        field_name is None and "new_quality_score" in value):
                    events.append(WebhookEvent(
                        kind="template_quality",
                        quality_score=_upper(value.get("new_quality_score")),
                        previous_quality_score=_upper(value.get("previous_quality_score")),
                        **tpl_keys))
                    continue
                if field_name == "template_category_update" or (
                        field_name is None and ("new_category" in value or "correct_category" in value)):
                    events.append(WebhookEvent(
                        kind="template_category",
                        category=_upper(value.get("new_category")),
                        previous_category=_upper(value.get("previous_category")),
                        # Meta's advance notice ("will be re-categorised") only
                        # carries correct_category; the category is not changed yet.
                        event=None if value.get("new_category") else (
                            "CORRECT_CATEGORY:" + str(_upper(value.get("correct_category")))
                            if value.get("correct_category") else None),
                        **tpl_keys))
                    continue
                if field_name == "message_template_status_update" or (
                        field_name is None and "message_template_name" in value):
                    other = value.get("other_info") if isinstance(value.get("other_info"), dict) else {}
                    reason = value.get("reason")
                    if not reason or str(reason).upper() == "NONE":
                        reason = other.get("description") or other.get("title") or None
                    events.append(WebhookEvent(
                        kind="template_status",
                        status=normalise_template_status(value.get("event")),
                        event=_upper(value.get("event")),
                        reason=reason,
                        category=_upper(value.get("message_template_category")),
                        **tpl_keys))
                    continue
                if field_name == "flows":
                    events.append(WebhookEvent(
                        kind="flow_event",
                        event=_upper(value.get("event")),
                        flow_id=str(value["flow_id"]) if value.get("flow_id") is not None else None,
                        alert_state=_upper(value.get("alert_state")),
                        reason=value.get("message"),
                        raw=value))
                    continue
                for msg in value.get("messages") or []:
                    # A reply: Meta's `button` (template quick reply),
                    # `interactive` or `text` message shapes.
                    if not isinstance(msg, dict):
                        continue
                    reply = (msg.get("interactive") or {}).get("button_reply") or {}
                    text = ((msg.get("button") or {}).get("text") or reply.get("title")
                            or (msg.get("text") or {}).get("body"))
                    events.append(WebhookEvent(
                        kind="inbound", text=text, provider_message_id=msg.get("id"),
                        recipient=str(msg.get("from") or "") or None, raw=msg))
                for st in value.get("statuses") or []:
                    if not isinstance(st, dict):
                        continue
                    opaque = _json_field(st.get("biz_opaque_callback_data"))
                    opaque = opaque if isinstance(opaque, dict) else {}
                    errors = st.get("errors") or []
                    err = errors[0] if errors and isinstance(errors[0], dict) else {}
                    ts = st.get("timestamp")
                    events.append(WebhookEvent(
                        kind="message_status",
                        status=str(st.get("status") or "").lower() or None,
                        provider_message_id=st.get("id"),
                        recipient=str(st.get("recipient_id") or "") or None,
                        timestamp=_int(ts) if ts else None,
                        error_code=str(err.get("code")) if err.get("code") is not None else None,
                        error_message=err.get("title") or err.get("message"),
                        batch_id=str(st.get("batchId") or opaque.get("batch_id")
                                     or top_batch or "") or None,
                        campaign_name=st.get("campaignName") or top_campaign,
                        raw=st,
                    ))
        return events
