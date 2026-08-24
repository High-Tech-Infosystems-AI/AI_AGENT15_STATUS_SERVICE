"""Known provider presets — host/port/security for common mail providers.

Served by ``GET /mail/providers`` so the UI can autofill the SMTP/IMAP fields.
The user can still override anything (provider='custom').
"""
from __future__ import annotations

# role → the standard system folders seeded on every account, in display order.
SYSTEM_FOLDERS = [
    ("inbox", "Inbox", 0),
    ("drafts", "Drafts", 1),
    ("sent", "Sent Items", 2),
    ("outbox", "Outbox", 3),      # local only — IMAP has no Outbox
    ("junk", "Junk Email", 4),
    ("archive", "Archive", 5),
    ("trash", "Deleted Items", 6),
]

PROVIDERS = {
    "gmail": {
        "label": "Gmail",
        "smtp_host": "smtp.gmail.com", "smtp_port": 587, "smtp_security": "starttls",
        "imap_host": "imap.gmail.com", "imap_port": 993, "imap_security": "ssl",
        "hint": "Gmail requires 2-step verification + a 16-character app password.",
    },
    "o365": {
        "label": "Outlook / Microsoft 365",
        "smtp_host": "smtp.office365.com", "smtp_port": 587, "smtp_security": "starttls",
        "imap_host": "outlook.office365.com", "imap_port": 993, "imap_security": "ssl",
        "hint": "Microsoft 365 / Outlook.com. May need an app password if MFA is on.",
    },
    "zoho": {
        "label": "Zoho Mail",
        "smtp_host": "smtp.zoho.com", "smtp_port": 465, "smtp_security": "ssl",
        "imap_host": "imap.zoho.com", "imap_port": 993, "imap_security": "ssl",
        "hint": "Generate an app-specific password in Zoho settings.",
    },
    "yahoo": {
        "label": "Yahoo Mail",
        "smtp_host": "smtp.mail.yahoo.com", "smtp_port": 465, "smtp_security": "ssl",
        "imap_host": "imap.mail.yahoo.com", "imap_port": 993, "imap_security": "ssl",
        "hint": "Yahoo needs an app password from Account Security.",
    },
    "custom": {
        "label": "Custom / Company",
        "smtp_host": "", "smtp_port": 587, "smtp_security": "starttls",
        "imap_host": "", "imap_port": 993, "imap_security": "ssl",
        "hint": "Enter your company's mail host settings.",
    },
}


def list_providers() -> list[dict]:
    return [{"key": k, **v} for k, v in PROVIDERS.items()]
