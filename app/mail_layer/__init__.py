"""Mail layer — per-user SMTP+IMAP mailboxes with Outlook-style folders,
messages, attachments and settings.

Runs as its own FastAPI app (``app.mail_main:app``) on port 8521 inside the
Status Service container, sharing the same SQLAlchemy Base / SessionLocal and
MySQL database as chat and status.

v1: per-user only. No admin / super-admin cross-mailbox access.
"""
