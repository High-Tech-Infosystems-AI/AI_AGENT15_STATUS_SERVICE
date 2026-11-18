"""Webhook URL checks.

Deliveries are sent from inside the cluster, so a URL must not reach the
cluster itself or any private network: every address its host resolves to has
to be a public one. The check runs when the URL is saved and again before each
delivery, since DNS can change in between.
"""
from __future__ import annotations

import ipaddress
import os
import socket
from typing import Optional
from urllib.parse import urlsplit

MAX_URL_LENGTH = 2048


def _allow_http() -> bool:
    # Local development only; production endpoints must use HTTPS.
    return os.getenv("WEBHOOK_ALLOW_HTTP", "").strip() == "1"


def _allow_private() -> bool:
    return os.getenv("WEBHOOK_ALLOW_PRIVATE_HOSTS", "").strip() == "1"


def url_problem(url: str) -> Optional[str]:
    """Why this URL can't receive webhooks, or None when it can."""
    url = (url or "").strip()
    if not url:
        return "Enter the URL to send events to."
    if len(url) > MAX_URL_LENGTH:
        return f"The URL must be at most {MAX_URL_LENGTH} characters."
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return "This is not a valid URL."
    if parts.scheme != "https" and not (parts.scheme == "http" and _allow_http()):
        return "The URL must start with https://."
    if not parts.hostname:
        return "The URL has no host name."
    if parts.username or parts.password:
        return "Remove the user name and password from the URL."
    if _allow_private():
        return None
    try:
        infos = socket.getaddrinfo(parts.hostname, port or (443 if parts.scheme == "https" else 80),
                                   proto=socket.IPPROTO_TCP)
    except (socket.gaierror, UnicodeError):
        return f"The host {parts.hostname} could not be found."
    for info in infos:
        address = ipaddress.ip_address(info[4][0].split("%")[0])
        if not address.is_global:
            return f"The host {parts.hostname} is on a private network; use a public URL."
    return None
