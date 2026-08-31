"""Helpers to turn raw RFC822 bytes into the fields we store, and to build
outbound MIME. Uses only the Python stdlib ``email`` package.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from email.header import decode_header, make_header
from email.message import Message
from email.utils import getaddresses, parsedate_to_datetime
from typing import Optional

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"[ \t\r\n ]+")


def decode_str(value: Optional[str]) -> str:
    """Decode an RFC2047 encoded header (=?utf-8?...?=) into a plain string."""
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:
        return value


def parse_addresses(raw) -> list[dict]:
    """Parse a To/Cc/From header value into [{name, address}]."""
    if not raw:
        return []
    if isinstance(raw, (list, tuple)):
        raw = ", ".join(str(r) for r in raw)
    out = []
    for name, addr in getaddresses([raw]):
        if not addr:
            continue
        out.append({"name": decode_str(name), "address": addr.lower()})
    return out


def html_to_text(html: str) -> str:
    """Best-effort HTML → plain text. Uses html2text if present, else a simple
    tag strip so the service works without the optional dependency."""
    if not html:
        return ""
    try:
        import html2text  # type: ignore

        h = html2text.HTML2Text()
        h.ignore_images = True
        h.body_width = 0
        return h.handle(html).strip()
    except Exception:
        text = _TAG_RE.sub(" ", html)
        return _WS_RE.sub(" ", text).strip()


def make_snippet(text: str, limit: int = 300) -> str:
    if not text:
        return ""
    flat = _WS_RE.sub(" ", text).strip()
    return flat[:limit]


def _parse_date(raw: Optional[str]) -> Optional[datetime]:
    if not raw:
        return None
    try:
        dt = parsedate_to_datetime(raw)
        if dt is None:
            return None
        if dt.tzinfo is not None:
            dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
        return dt
    except Exception:
        return None


def parse_message(raw_bytes: bytes) -> dict:
    """Parse raw RFC822 bytes into the dict we persist to mail_messages.

    Returns keys aligned with MailMessage columns plus an ``attachments`` list
    of {file_name, mime_type, size_bytes, content_id, is_inline, data}.
    """
    from email import message_from_bytes
    from email import policy

    msg: Message = message_from_bytes(raw_bytes, policy=policy.default)

    from_list = parse_addresses(msg.get("From"))
    from_name = from_list[0]["name"] if from_list else ""
    from_address = from_list[0]["address"] if from_list else ""

    body_text, body_html = "", ""
    attachments: list[dict] = []

    if msg.is_multipart():
        for part in msg.walk():
            if part.is_multipart():
                continue
            ctype = (part.get_content_type() or "").lower()
            disp = (part.get("Content-Disposition") or "").lower()
            filename = part.get_filename()
            if filename or "attachment" in disp or (
                ctype not in ("text/plain", "text/html") and part.get("Content-ID")
            ):
                try:
                    payload = part.get_payload(decode=True) or b""
                except Exception:
                    payload = b""
                cid = (part.get("Content-ID") or "").strip("<>") or None
                attachments.append({
                    "file_name": decode_str(filename) or (cid or "attachment"),
                    "mime_type": ctype or "application/octet-stream",
                    "size_bytes": len(payload),
                    "content_id": cid,
                    "is_inline": 1 if "inline" in disp or cid else 0,
                    "data": payload,
                })
                continue
            try:
                content = part.get_content()
            except Exception:
                content = part.get_payload(decode=True)
                content = content.decode("utf-8", "replace") if content else ""
            if ctype == "text/plain" and not body_text:
                body_text = content or ""
            elif ctype == "text/html" and not body_html:
                body_html = content or ""
    else:
        ctype = (msg.get_content_type() or "").lower()
        try:
            content = msg.get_content()
        except Exception:
            payload = msg.get_payload(decode=True)
            content = payload.decode("utf-8", "replace") if payload else ""
        if ctype == "text/html":
            body_html = content or ""
        else:
            body_text = content or ""

    if not body_text and body_html:
        body_text = html_to_text(body_html)

    references = msg.get("References") or ""
    in_reply_to = (msg.get("In-Reply-To") or "").strip()
    # Conversation key = root of the References chain, else this Message-ID.
    conv = None
    ids = re.findall(r"<[^>]+>", references)
    if ids:
        conv = ids[0].strip("<>")
    elif in_reply_to:
        conv = in_reply_to.strip("<>")
    else:
        conv = (msg.get("Message-ID") or "").strip().strip("<>") or None

    return {
        "message_id_hdr": (msg.get("Message-ID") or "").strip(),
        "conversation_id": (conv or "")[:255] or None,
        "in_reply_to": in_reply_to[:512] or None,
        "references_hdr": references or None,
        "from_name": from_name[:255],
        "from_address": from_address[:320],
        "to_json": parse_addresses(msg.get_all("To")),
        "cc_json": parse_addresses(msg.get_all("Cc")),
        "reply_to_address": (parse_addresses(msg.get("Reply-To")) or [{}])[0].get("address"),
        "subject": decode_str(msg.get("Subject"))[:998],
        "body_text": body_text,
        "body_html": body_html or None,
        "importance": _importance(msg),
        "has_attachments": 1 if attachments else 0,
        "size_bytes": len(raw_bytes),
        "sent_at": _parse_date(msg.get("Date")),
        "attachments": attachments,
    }


def _importance(msg: Message) -> str:
    imp = (msg.get("Importance") or "").lower()
    if "high" in imp:
        return "high"
    if "low" in imp:
        return "low"
    prio = (msg.get("X-Priority") or "").strip()
    if prio.startswith("1") or prio.startswith("2"):
        return "high"
    if prio.startswith("4") or prio.startswith("5"):
        return "low"
    return "normal"
