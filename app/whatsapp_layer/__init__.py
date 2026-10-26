"""Clawback WhatsApp notifications.

Provider accounts, WhatsApp templates, the clawback reminder / digest engine,
health checks and message logs. Mounted by app.main under /status/whatsapp
and /status/clawback-notifications.

Design: CLAWBACK_WHATSAPP_NOTIFICATIONS_PLAN.md; API contract:
CLAWBACK_WHATSAPP_API_CONTRACT.md (the contract wins where they differ).
"""
