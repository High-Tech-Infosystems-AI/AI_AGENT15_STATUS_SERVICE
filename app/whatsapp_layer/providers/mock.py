"""Mock provider (WHATSAPP_PROVIDER_MODE=mock).

Simulates the provider without any network call so the engine, API and UI
can be built and tested before the vendor gaps (plan §4.4) are closed.
Outcome is chosen by the destination number's last digits:

  ...0000  → failed (provider reported the number as failed)
  ...1111  → duplicate (skipped / provider_duplicate)
  ...5000  → retryable 5xx
  ...4010  → 401 (account becomes auth_error)
  anything else → accepted, batch Done on the next poll
"""
from __future__ import annotations

import itertools
import logging
import time

from app.whatsapp_layer.providers.base import (
    AccountHealth, ProviderTemplate, SendResult, SendStatus, WhatsAppProvider,
)
from app.whatsapp_layer.providers.omnichannel import OmnichannelProvider

logger = logging.getLogger("app_logger")

_counter = itertools.count(1)


class MockProvider(WhatsAppProvider):
    key = "mock"
    label = "Mock"

    def send_template(self, creds, to_digits, template, header_vars, body_vars,
                      correlation_id) -> SendResult:
        digits = str(to_digits or "")
        logger.info("[whatsapp-mock] send %s to ...%s (%s)", template.name,
                    digits[-4:], correlation_id)
        if digits.endswith("0000"):
            return SendResult(accepted=False, error_code="provider_failed",
                              error_message="Mock: number failed")
        if digits.endswith("1111"):
            return SendResult(accepted=False, duplicate=True, error_code="duplicate",
                              error_message="Mock: duplicate")
        if digits.endswith("5000"):
            return SendResult(accepted=False, error_code="503", retryable=True,
                              error_message="Mock: provider HTTP 503")
        if digits.endswith("4010"):
            return SendResult(accepted=False, error_code="401", auth_error=True,
                              error_message="Mock: provider rejected the access token")
        batch = f"mock{int(time.time())}{next(_counter):06d}"
        return SendResult(accepted=True, provider_ref=batch,
                          raw={"totalNumbersSuccess": 1, "totalNumbersFailed": 0,
                               "totalNumbersDuplicate": 0, "batchId": batch})

    def create_template(self, creds, spec) -> ProviderTemplate:
        return ProviderTemplate(name=spec.name, language=spec.language,
                                provider_template_id=f"mock-{next(_counter)}",
                                status="pending", category=spec.category)

    def get_template(self, creds, name, language):
        # Mock approves everything on the first sync.
        return ProviderTemplate(name=name, language=language, status="approved")

    def list_templates(self, creds, category=None):
        return []

    def delete_template(self, creds, name) -> None:
        return None

    def get_send_status(self, creds, provider_ref) -> SendStatus:
        return SendStatus(status="sent", processed=1, pending=0)

    def parse_webhook(self, headers, body):
        # Same best-effort mapping as the live adapter.
        return OmnichannelProvider.parse_webhook(OmnichannelProvider(), headers, body)

    def health_check(self, creds) -> AccountHealth:
        return AccountHealth(ok=True, status="ACTIVE", quality_rating="GREEN",
                             messaging_limit="TIER_1K", sandbox=True)
