"""@username mention extraction."""
import re
from typing import List

_CODE_BLOCK_RE = re.compile(r"```.*?```", re.DOTALL)
# Username after a non-word boundary, prefixed by @, not preceded by alpha-num (skip emails)
_MENTION_RE = re.compile(r"(?<![\w@])@([a-zA-Z][a-zA-Z0-9_]{1,49})")


def extract_usernames(text: str) -> List[str]:
    """Return distinct lowercase usernames mentioned in text. Skips fenced code blocks and emails."""
    if not text:
        return []
    stripped = _CODE_BLOCK_RE.sub("", text)
    seen = []
    for match in _MENTION_RE.finditer(stripped):
        u = match.group(1).lower()
        if u not in seen:
            seen.append(u)
    return seen
