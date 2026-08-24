"""SMTP sending + connection testing (Python stdlib smtplib)."""
from __future__ import annotations

import logging
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from typing import List, Optional, Tuple

logger = logging.getLogger("app_logger")

_TIMEOUT = 20


def _connect(host: str, port: int, security: str, timeout: int = _TIMEOUT):
    security = (security or "starttls").lower()
    if security == "ssl":
        ctx = ssl.create_default_context()
        return smtplib.SMTP_SSL(host, port, timeout=timeout, context=ctx)
    server = smtplib.SMTP(host, port, timeout=timeout)
    if security == "starttls":
        server.ehlo()
        server.starttls(context=ssl.create_default_context())
        server.ehlo()
    return server


def test_smtp(host: str, port: int, security: str, username: str,
              password: str) -> Tuple[bool, str]:
    """Return (ok, detail) after attempting login. Never raises."""
    try:
        server = _connect(host, port, security)
    except Exception as exc:
        return False, f"connect failed: {exc}"
    try:
        server.login(username, password)
        return True, "authenticated"
    except smtplib.SMTPAuthenticationError:
        return False, "authentication rejected"
    except Exception as exc:
        return False, f"login failed: {exc}"
    finally:
        try:
            server.quit()
        except Exception:
            pass


def build_message(*, from_addr: str, from_name: str,
                  to: List[str], cc: List[str], bcc: List[str],
                  subject: str, body_html: Optional[str],
                  body_text: Optional[str],
                  in_reply_to: Optional[str] = None,
                  references: Optional[str] = None,
                  attachments: Optional[List[dict]] = None,
                  request_read_receipt: bool = False,
                  domain: Optional[str] = None) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = f"{from_name} <{from_addr}>" if from_name else from_addr
    if to:
        msg["To"] = ", ".join(to)
    if cc:
        msg["Cc"] = ", ".join(cc)
    msg["Subject"] = subject or ""
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=domain or (from_addr.split("@")[-1] if "@" in from_addr else None))
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = references or in_reply_to
    if request_read_receipt:
        msg["Disposition-Notification-To"] = from_addr

    text = body_text or ""
    if body_html:
        msg.set_content(text or " ")
        msg.add_alternative(body_html, subtype="html")
    else:
        msg.set_content(text or " ")

    for att in (attachments or []):
        data = att.get("data") or b""
        mime = (att.get("mime_type") or "application/octet-stream")
        maintype, _, subtype = mime.partition("/")
        msg.add_attachment(data, maintype=maintype or "application",
                           subtype=subtype or "octet-stream",
                           filename=att.get("file_name") or "attachment")
    return msg


def send(*, host: str, port: int, security: str, username: str, password: str,
         msg: EmailMessage, from_addr: str,
         recipients: List[str]) -> Tuple[bool, str]:
    """Send an already-built EmailMessage. Returns (ok, detail)."""
    try:
        server = _connect(host, port, security)
    except Exception as exc:
        return False, f"connect failed: {exc}"
    try:
        server.login(username, password)
        server.send_message(msg, from_addr=from_addr, to_addrs=recipients)
        return True, "sent"
    except Exception as exc:
        logger.warning("SMTP send failed: %s", exc)
        return False, str(exc)
    finally:
        try:
            server.quit()
        except Exception:
            pass
