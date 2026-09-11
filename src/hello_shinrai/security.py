from __future__ import annotations

import hashlib
import ipaddress
import re
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

SECRET_FIELDS = {
    "api_key",
    "key",
    "shinrai_key",
    "llm_key",
    "azure_key",
    "secret",
    "secretaccesskey",
    "authorization",
    "ocp-apim-subscription-key",
    "x-goog-api-key",
    "x-api-key",
}
SIGNED_QUERY = {"sig", "signature", "x-amz-signature", "se", "sp", "sv", "sr", "skoid", "sktid"}


def validate_base_url(value: str, *, allow_path: bool = False) -> str:
    parsed = urlsplit(value.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("Enter an HTTP or HTTPS URL without embedded credentials.")
    try:
        loopback = ipaddress.ip_address(parsed.hostname).is_loopback
    except ValueError:
        loopback = parsed.hostname.lower() == "localhost"
    if parsed.scheme == "http" and not loopback:
        raise ValueError("Remote endpoints must use HTTPS. HTTP is allowed only on this computer.")
    if parsed.query or parsed.fragment or (not allow_path and parsed.path not in {"", "/"}):
        raise ValueError("The base URL must not contain a path, query, or fragment.")
    path = parsed.path.rstrip("/") if allow_path else ""
    return urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


def redact_url(value: str) -> str:
    try:
        parsed = urlsplit(value)
        query = [(key, "[hidden]" if key.lower() in SIGNED_QUERY else val) for key, val in parse_qsl(parsed.query)]
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query), ""))
    except ValueError:
        return "[invalid URL]"


def sanitize(value: Any, *, include_sensitive: bool = False) -> Any:
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            lowered = str(key).lower().replace("-", "_")
            if lowered in {field.replace("-", "_") for field in SECRET_FIELDS}:
                result[key] = "[hidden]"
            elif not include_sensitive and lowered in {
                "mapping",
                "known_replacements",
                "original",
                "original_input",
                "original_body",
            }:
                result[key] = "[omitted from safe export]"
            else:
                result[key] = sanitize(item, include_sensitive=include_sensitive)
        return result
    if isinstance(value, list):
        return [sanitize(item, include_sensitive=include_sensitive) for item in value]
    if isinstance(value, bytes):
        return {"binary": True, "bytes": len(value), "sha256": hashlib.sha256(value).hexdigest()}
    if isinstance(value, str):
        if value.startswith(("http://", "https://")):
            return redact_url(value)
        return re.sub(r"Bearer\s+[A-Za-z0-9._~+\-/=]+", "Bearer [hidden]", value, flags=re.IGNORECASE)
    return value
