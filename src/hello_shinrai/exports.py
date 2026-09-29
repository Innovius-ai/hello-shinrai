"""Request collections for the vendor compatibility APIs, built from the reviewed catalogue.

The collections never contain a key. `.http` files use the {{SHINRAI_API_KEY}} variable of the
HTTP client environment; curl files read the SHINRAI_API_KEY environment variable. A collection
for the local vendor endpoints needs no key, because the endpoints add the saved key.
"""

from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import urlencode

from .catalog import OPERATIONS
from .client import AWS_CONTENT_TYPE, AWS_PATH, AWS_REGION, AWS_SERVICE, vendor_path
from .vendor_ports import VENDOR_NAMES

GROUPS = {"azure": "Azure Language", "google": "Google DLP", "aws": "AWS Comprehend"}
KEY_HEADERS = {"azure": "Ocp-Apim-Subscription-Key", "google": "x-goog-api-key", "bearer": "Authorization"}
PLACEHOLDER = re.compile(r"\{([A-Za-z_]+)\}")
HTTP_KEY = "{{SHINRAI_API_KEY}}"
CURL_KEY = "${SHINRAI_API_KEY:?Set SHINRAI_API_KEY}"
CURL_AWS_USER = (
    "${SHINRAI_AWS_ACCESS_KEY_ID:?Run aws_credentials and set SHINRAI_AWS_ACCESS_KEY_ID}:"
    "${SHINRAI_AWS_SECRET_ACCESS_KEY:?Run aws_credentials and set SHINRAI_AWS_SECRET_ACCESS_KEY}"
)


def entries(vendor: str, *, local: bool) -> list[dict[str, Any]]:
    """The catalogue operations of one vendor. The local endpoint serves the vendor paths only."""
    rows = []
    for operation in OPERATIONS:
        if operation["group"] != GROUPS[vendor]:
            continue
        if local and operation["auth"] != vendor:
            continue  # credential and artifact routes use the ShinrAI key: not on the local endpoint
        rows.append(operation)
    return rows


def request_path(operation: dict[str, Any], vendor: str) -> tuple[str, str]:
    """(variable, path): vendor operations are relative to the vendor base, the others to ShinrAI."""
    if operation["auth"] == vendor:
        return "base", "" if operation["path"] == "/" else operation["path"]
    return "shinrai", vendor_path(operation["path"])


def query_string(operation: dict[str, Any]) -> str:
    query = operation.get("query") or {}
    return ("?" + urlencode(query)) if query else ""


def body_text(operation: dict[str, Any]) -> str | None:
    body = operation.get("body")
    return None if body is None else json.dumps(body, indent=2, ensure_ascii=False)


def vendor_base(vendor: str, shinrai_url: str, local_endpoint: str | None) -> str:
    if local_endpoint:
        return local_endpoint
    return shinrai_url + (AWS_PATH.rstrip("/") if vendor == "aws" else f"/v1/{vendor}")


def intro(vendor: str, *, local: bool, version: str, marker: str) -> list[str]:
    lines = [f"{marker} ShinrAI · {VENDOR_NAMES[vendor]} compatibility · exported by Hello ShinrAI {version}"]
    if local:
        lines += [
            f"{marker} Target: the local vendor endpoint of Hello ShinrAI. It adds your saved ShinrAI key.",
            f"{marker} It works only while this Hello ShinrAI run keeps the vendor endpoints on.",
        ]
    else:
        lines.append(f"{marker} Target: the {VENDOR_NAMES[vendor]} compatibility API of your ShinrAI deployment.")
    return lines


def http_collection(vendor: str, *, shinrai_url: str, local_endpoint: str | None, version: str) -> str:
    local = local_endpoint is not None
    lines = intro(vendor, local=local, version=version, marker="#")
    if not local:
        lines.append("# Define SHINRAI_API_KEY in your HTTP client environment. This file does not contain the key.")
        if vendor == "aws":
            lines += [
                "# Sign the Comprehend requests with AWS Signature Version 4 (service comprehend, any region)",
                "# and the pair from the credentials request. The curl export and the local AWS endpoint sign for you.",
            ]
    lines += ["", f"@base = {vendor_base(vendor, shinrai_url, local_endpoint)}"]
    if not local:
        lines.append(f"@shinrai = {shinrai_url}")
    for operation in entries(vendor, local=local):
        variable, path = request_path(operation, vendor)
        lines += ["", f"### {operation['name']}"]
        if operation.get("requires"):
            lines.append(f"# {operation['requires']}.")
        url = "{{" + variable + "}}" + (path or ("" if local else "/")) + query_string(operation)
        lines.append(f"{operation['method']} {url}")
        auth = operation["auth"]
        if auth == "aws":
            lines += [f"Content-Type: {AWS_CONTENT_TYPE}", f"X-Amz-Target: {operation['aws_target']}"]
        elif operation.get("body") is not None:
            lines.append("Content-Type: application/json")
        if not local and auth in KEY_HEADERS:
            value = ("Bearer " if auth == "bearer" else "") + HTTP_KEY
            lines.append(f"{KEY_HEADERS[auth]}: {value}")
        body = body_text(operation)
        if body is not None:
            lines += ["", body]
    return "\n".join(lines) + "\n"


def shell_double_quoted(value: str) -> str:
    """Text for a double-quoted shell word: no expansion of the literal parts."""
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("$", "\\$").replace("`", "\\`")


def shell_single_quoted(value: str) -> str:
    return "'" + value.replace("'", "'\"'\"'") + "'"


def curl_collection(vendor: str, *, shinrai_url: str, local_endpoint: str | None, version: str) -> str:
    local = local_endpoint is not None
    example = next(operation for operation in entries(vendor, local=local) if operation["auth"] == vendor)
    lines = ["#!/bin/sh", *intro(vendor, local=local, version=version, marker="#")]
    lines += [
        f"# Usage: . ./{filename(vendor, 'curl', local)}, then run one function, for example {function_name(example)}.",
        "# Functions for paths with placeholders take the values as arguments, in path order.",
    ]
    if not local:
        lines.append("# The key comes from the SHINRAI_API_KEY environment variable. This file does not contain it.")
        if vendor == "aws":
            lines += [
                "# Comprehend calls need curl 8 or later (--aws-sigv4). Run aws_credentials once, then set",
                "# SHINRAI_AWS_ACCESS_KEY_ID and SHINRAI_AWS_SECRET_ACCESS_KEY to the returned pair.",
            ]
    lines += ["", f"BASE={shell_single_quoted(vendor_base(vendor, shinrai_url, local_endpoint))}"]
    if not local:
        lines.append(f"SHINRAI={shell_single_quoted(shinrai_url)}")
    for operation in entries(vendor, local=local):
        variable, path = request_path(operation, vendor)
        literal = shell_double_quoted((path or ("" if local else "/")) + query_string(operation))
        url = '"$' + variable.upper() + positional(literal) + '"'
        command = [f"curl -sS -X {operation['method']} {url}"]
        auth = operation["auth"]
        if auth == "aws":
            if not local:
                command.append(f"--aws-sigv4 'aws:amz:{AWS_REGION}:{AWS_SERVICE}'")
                command.append(f'--user "{CURL_AWS_USER}"')
            command += [f"-H 'Content-Type: {AWS_CONTENT_TYPE}'", f"-H 'X-Amz-Target: {operation['aws_target']}'"]
        elif operation.get("body") is not None:
            command.append("-H 'Content-Type: application/json'")
        if not local and auth in KEY_HEADERS:
            value = ("Bearer " if auth == "bearer" else "") + CURL_KEY
            command.append(f'-H "{KEY_HEADERS[auth]}: {value}"')
        body = body_text(operation)
        if body is not None:
            command.append("--data-binary @- <<'JSON'")
        lines += ["", f"# {operation['name']}"]
        if operation.get("requires"):
            lines.append(f"# {operation['requires']}.")
        lines.append(f"{function_name(operation)}() {{")
        lines.append("  " + " \\\n    ".join(command))
        if body is not None:
            lines += [body, "JSON"]
        lines.append("}")
    return "\n".join(lines) + "\n"


def positional(text: str) -> str:
    """{name} placeholders as required shell arguments: ${1:?Pass name}, ${2:?Pass other}, ..."""
    names = PLACEHOLDER.findall(text)
    for number, name in enumerate(names, start=1):
        text = text.replace("{" + name + "}", f"${{{number}:?Pass {name}}}", 1)
    return text


def filename(vendor: str, fmt: str, local: bool) -> str:
    return f"shinrai-{vendor}-{'local' if local else 'deployment'}.{'http' if fmt == 'http' else 'sh'}"


def function_name(operation: dict[str, Any]) -> str:
    return re.sub(r"[^a-z0-9]+", "_", operation["id"].lower()).strip("_")


def collection(vendor: str, *, fmt: str, shinrai_url: str, local_endpoint: str | None, version: str) -> str:
    build = http_collection if fmt == "http" else curl_collection
    return build(vendor, shinrai_url=shinrai_url, local_endpoint=local_endpoint, version=version)
