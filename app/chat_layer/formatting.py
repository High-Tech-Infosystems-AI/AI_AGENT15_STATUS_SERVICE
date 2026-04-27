"""WhatsApp-style formatting sanitiser. Server doesn't render — just hardens against XSS."""
import re

_SCRIPT_RE = re.compile(r"<script\b[^>]*>.*?</script\s*>", re.IGNORECASE | re.DOTALL)
_TAG_RE = re.compile(r"<[^>]*>")


def sanitise_body(text: str) -> str:
    """Remove dangerous HTML and escape lt/gt while preserving WhatsApp formatting markers."""
    if not text:
        return text
    text = _SCRIPT_RE.sub("", text)
    # Strip any remaining HTML tags (paranoid)
    text = _TAG_RE.sub("", text)
    # Escape literal angle brackets so frontends rendering as text don't reinterpret them
    text = text.replace("<", "&lt;").replace(">", "&gt;")
    return text
