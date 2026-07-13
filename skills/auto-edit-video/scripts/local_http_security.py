#!/usr/bin/env python3
"""Shared loopback HTTP trust checks for Auto Edit Studio services."""

from __future__ import annotations

import hmac
import urllib.parse
from collections.abc import Mapping


LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


def is_loopback_host(host: str) -> bool:
    return host.strip().lower() in LOOPBACK_HOSTS


def host_header_allowed(bound_host: str, host_header: str) -> bool:
    """Reject DNS-rebinding Host headers when a service is loopback-bound."""
    if not is_loopback_host(bound_host):
        return True
    try:
        requested_host = urllib.parse.urlsplit(f"//{host_header}").hostname
    except ValueError:
        return False
    return (requested_host or "").lower() in LOOPBACK_HOSTS


def mutation_origin_allowed(headers: Mapping[str, str]) -> bool:
    """Permit CLI writes without Origin and same-origin browser writes only."""
    if headers.get("Sec-Fetch-Site", "").lower() == "cross-site":
        return False
    origin = headers.get("Origin")
    if not origin:
        return True
    if origin == "null":
        return False
    try:
        parsed = urllib.parse.urlsplit(origin)
    except ValueError:
        return False
    return (
        parsed.scheme == "http"
        and parsed.netloc.lower() == headers.get("Host", "").lower()
    )


def csrf_token_matches(headers: Mapping[str, str], expected: str) -> bool:
    supplied = headers.get("X-Auto-Edit-CSRF", "")
    return bool(supplied) and hmac.compare_digest(supplied, expected)
