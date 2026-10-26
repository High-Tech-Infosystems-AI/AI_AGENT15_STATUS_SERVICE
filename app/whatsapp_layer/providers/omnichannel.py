"""Omnichannel (BroadNet CPaaS / "Advance Messaging Systems") WhatsApp API.

Documented and used here (vendor doc v1.0.0):
  - POST {base}/whatsapp/message/sendbulk      multipart, messageType=TEMPLATE
  - POST {base}/whatsapp/template              multipart, create template
  - GET  {base}/whatsapp/message/edit?batch_Id= batch status
  - GET  {base}/whatsapp/clients-onboard/me    account health
Auth is `Authorization: Bearer <User-Access-Token>` everywhere.

NOT documented (index lists §3.24–3.27 but the body pages are missing), so
get/list/delete template are best-effort guesses isolated in one block below,
and the webhook payload mapping is a best-effort Meta Cloud API mapping.
Both are marked `# UNCONFIRMED` and must be checked with the vendor
(plan §4.4) before go-live.

Correlation: one send = one BroadNet batch (batchId = provider_ref), and we
set campaignName = our correlation id ("cbk_<log_id>").
"""
from __future__ import annotations

import json
import logging
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
    "LIMIT_EXCEEDED": "paused",
}


def normalise_template_status(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    return _TEMPLATE_STATUS_MAP.get(str(value).strip().upper())


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
        for i, value in enumerate(body_vars or []):
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

    # ------------------------------------------------------------------
    # Templates
    # ------------------------------------------------------------------
    @staticmethod
    def build_template_fields(spec: TemplateSpec) -> List[Tuple[str, Tuple[None, str]]]:
        fields: List[Tuple[str, str]] = [
            ("name", spec.name),
            ("languageType", spec.language),
            ("category", spec.category or "UTILITY"),
            ("templateType", "DEFAULT"),
            ("subCategory", "DEFAULT"),
            ("headerFormat", spec.header_format or "NONE"),
            ("bodyText", spec.body_text),
        ]
        if (spec.header_format or "NONE") == "TEXT" and spec.header_text:
            fields.append(("headerText", spec.header_text))
            if spec.header_example:
                fields.append(("headerExample", json.dumps(list(spec.header_example))))
        if spec.body_example:
            fields.append(("bodyExample", json.dumps([list(spec.body_example)])))
        if spec.footer_text:
            fields.append(("footerText", spec.footer_text))
        # UNCONFIRMED: the doc shows one `buttonRequest` string
        # ("TYPE:Label|value"); for several buttons we repeat the field.
        for b in spec.buttons or []:
            btype = (b.get("type") or "").upper()
            label = b.get("label") or ""
            if btype == "URL":
                fields.append(("buttonRequest", f"URL:{label}|{b.get('url') or ''}"))
            elif btype == "QUICK_REPLY":
                fields.append(("buttonRequest", f"QUICK_REPLY:{label}"))
        return [(k, (None, v)) for k, v in fields]

    @staticmethod
    def parse_template(body: Any) -> ProviderTemplate:
        body = body if isinstance(body, dict) else {}
        reason = body.get("rejectionReason")
        if not reason and body.get("rejectionInfoJson"):
            reason = str(body.get("rejectionInfoJson"))[:500]
        pid = body.get("id")
        return ProviderTemplate(
            name=body.get("name"),
            language=body.get("language") or body.get("languageType"),
            provider_template_id=str(pid) if pid not in (None, "") else None,
            status=normalise_template_status(body.get("status")),
            category=body.get("category"),
            previous_category=body.get("previousCategory"),
            rejection_reason=reason,
            raw=body,
        )

    def create_template(self, creds, spec) -> ProviderTemplate:
        resp = self._request("POST", creds, "/whatsapp/template",
                             files=self.build_template_fields(spec))
        self._raise_for_status(resp)
        return self.parse_template(self._json(resp))

    # --- UNCONFIRMED: vendor doc §3.24–3.27 missing ---------------------
    # Paths below are guesses modelled on the documented create endpoint.
    # A 404 / 405 surfaces as ProviderUnsupported so the sync task can treat
    # "no status API" as a no-op instead of an error.
    def get_template(self, creds, name, language) -> Optional[ProviderTemplate]:
        # UNCONFIRMED: vendor doc §3.24–3.27 missing
        resp = self._request("GET", creds, f"/whatsapp/template/{name}")
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

    def list_templates(self, creds, category=None) -> List[ProviderTemplate]:
        # UNCONFIRMED: vendor doc §3.24–3.27 missing
        params = {"category": category} if category else None
        resp = self._request("GET", creds, "/whatsapp/template", params=params)
        if resp.status_code in (404, 405, 501):
            raise ProviderUnsupported("Template list API not available",
                                      status_code=resp.status_code)
        self._raise_for_status(resp)
        body = self._json(resp)
        items = body if isinstance(body, list) else (
            (body.get("data") or body.get("content") or []) if isinstance(body, dict) else [])
        return [self.parse_template(i) for i in items if isinstance(i, dict)]

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

    def get_send_status(self, creds, provider_ref) -> SendStatus:
        resp = self._request("GET", creds, "/whatsapp/message/edit",
                             params={"batch_Id": provider_ref})
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
        # UNCONFIRMED: the vendor has not documented the webhook payload
        # (plan §4.4 #3). This maps the Meta Cloud API shape, which BroadNet
        # most likely forwards:
        #   {"entry":[{"changes":[{"field":"messages","value":{"statuses":[
        #       {"id":"wamid..","status":"delivered","recipient_id":"91..",
        #        "timestamp":"169..","errors":[{"code":131026,"title":".."}]}]}}]}]}
        #   {"entry":[{"changes":[{"field":"message_template_status_update",
        #       "value":{"event":"APPROVED","message_template_id":123,
        #        "message_template_name":"x","message_template_language":"en_US",
        #        "reason":"NONE"}}]}]}
        # plus flat {"statuses":[...]} / top-level batchId|campaignName keys.
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
                if field_name == "message_template_status_update" or "message_template_name" in value:
                    events.append(WebhookEvent(
                        kind="template_status",
                        status=normalise_template_status(value.get("event")),
                        template_name=value.get("message_template_name"),
                        template_language=value.get("message_template_language"),
                        provider_template_id=(str(value["message_template_id"])
                                              if value.get("message_template_id") is not None else None),
                        reason=value.get("reason"),
                        raw=value,
                    ))
                    continue
                for st in value.get("statuses") or []:
                    if not isinstance(st, dict):
                        continue
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
                        batch_id=str(st.get("batchId") or top_batch or "") or None,
                        campaign_name=st.get("campaignName") or top_campaign,
                        raw=st,
                    ))
        return events
