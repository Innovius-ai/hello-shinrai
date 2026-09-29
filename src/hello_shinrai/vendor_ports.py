"""Local vendor endpoints: loopback listeners for unchanged Azure, Google, and AWS SDKs.

The listeners are off by default and are never persisted. They bind 127.0.0.1 only and accept
requests under a path prefix drawn at random for each run (SDKs accept an endpoint with a path,
but cannot send a session token). Each request goes to the configured ShinrAI deployment under
/v1/<vendor> with the saved ShinrAI key: Azure receives Ocp-Apim-Subscription-Key, Google
x-goog-api-key, and AWS a new SigV4 signature with the pair ShinrAI issued for the key. The
client's own credentials are dropped. Traces keep the method, path, status, time, and vendor
headers, never the bodies. The responses carry no CORS headers.
"""

from __future__ import annotations

import asyncio
import contextlib
import copy
import json
import re
import secrets
import socket
import sys
import time
import uuid
from typing import Any
from urllib.parse import unquote, urlsplit

import httpx
import uvicorn

from .client import AWS_CONTENT_TYPE, VENDOR_GOOGLE, RemoteError, ShinraiClient, aws_signed

VENDORS = ("azure", "google", "aws")
VENDOR_NAMES = {"azure": "Azure AI Language PII", "google": "Google Cloud DLP", "aws": "AWS Comprehend PII"}
# The tags of the vendor operations in the deployment's OpenAPI document.
VENDOR_TAGS = {"azure": "Azure compatibility", "google": "Google compatibility", "aws": "AWS compatibility"}
DEFAULT_BASE_PORT = 8210
PREFIXED = re.compile(r"^/v1/(azure|google|aws)(/.*)?$")
MAX_BODY_BYTES = 20 * 1024 * 1024
TIMEOUT_S = 300
TRACE_LIMIT = 100
ALLOWED_METHODS = {
    "azure": {"GET", "POST", "PUT", "PATCH", "DELETE"},
    "google": {"GET", "POST", "PATCH", "DELETE"},
    "aws": {"POST"},
}
# Request headers passed on. Credentials of the client are never passed on.
FORWARDED_HEADERS = {
    "azure": ("content-type", "accept", "x-ms-client-request-id"),
    "google": ("content-type", "accept"),
    "aws": (),
}
# Query parameters that carry credentials in the vendor APIs.
DROPPED_QUERY = {"azure": {"subscription-key"}, "google": {"key", "access_token"}, "aws": set()}
RETURNED_HEADERS = {
    "content-type",
    "operation-location",
    "retry-after",
    "x-ms-request-id",
    "apim-request-id",
    "x-request-id",
    "x-records-charged",
    "x-records-remaining",
    "x-shinrai-warnings",
    "x-shinrai-unmapped-entity-types",
    "x-amzn-requestid",
    "x-amzn-errortype",
}
AWS_TARGET = re.compile(r"^[A-Za-z0-9_]{1,64}\.[A-Za-z0-9]{1,64}$")
LINK_KEYS = {"nextLink", "@nextLink"}


class VendorPortError(RuntimeError):
    """The listeners could not start: a port is in use or out of range."""


class Refused(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


def vendor_of_path(path: str, method: str, headers: dict[str, str]) -> str | None:
    """The vendor API a path belongs to (the rule of the ShinrAI gateway)."""
    if path.startswith(("/language/", "/text/analytics/")):
        return "azure"
    if path.startswith(VENDOR_GOOGLE):
        return "google"
    if path.startswith("/providers/aws/") or (path == "/" and method == "POST" and "x-amz-target" in headers):
        return "aws"
    return None


def vendor_openapi(document: Any, vendor: str, *, server_url: str, local: bool) -> dict[str, Any]:
    """One vendor's operations from a deployment's OpenAPI document, at the vendor's own paths.

    The hosted API publishes the vendor paths under /v1/<vendor>; an offline installation may
    publish them at the root. Both give the same result. For the local endpoint the security
    requirements go away, because the endpoint adds the saved key.
    """
    if not isinstance(document, dict) or not isinstance(document.get("paths"), dict):
        raise TypeError("The deployment returned no OpenAPI document.")
    doc = copy.deepcopy(document)
    paths: dict[str, Any] = {}
    for path, item in doc["paths"].items():
        if not isinstance(path, str) or not isinstance(item, dict):
            continue
        match = PREFIXED.match(path)
        if match and match.group(1) != vendor:
            continue
        own = (match.group(2) or "/") if match else path
        if vendor_of_path(own, "POST", {"x-amz-target": "*"}) != vendor:
            continue
        if local and (vendor == "aws" and own != "/"):
            continue  # the local AWS endpoint signs for the client: no credential route
        if local:
            for operation in item.values():
                if isinstance(operation, dict):
                    operation.pop("security", None)
        paths[own] = item
    if not paths:
        raise LookupError(f"The deployment publishes no {VENDOR_NAMES[vendor]} compatibility operations.")
    doc["paths"] = paths
    doc["tags"] = [
        tag for tag in doc.get("tags") or [] if isinstance(tag, dict) and tag.get("name") == VENDOR_TAGS[vendor]
    ]
    info = doc.setdefault("info", {})
    info["title"] = f"ShinrAI · {VENDOR_NAMES[vendor]} compatibility"
    if local:
        doc.pop("security", None)
        components = doc.get("components")
        if isinstance(components, dict):
            components.pop("securitySchemes", None)
        doc["servers"] = [{"url": server_url, "description": "Local vendor endpoint of Hello ShinrAI"}]
        info["description"] = (
            f"The {VENDOR_NAMES[vendor]} compatibility API through the local vendor endpoint of Hello ShinrAI. "
            "The endpoint adds your saved ShinrAI key, so clients need no ShinrAI key. SDKs that require a key or "
            "an AWS key pair can use any placeholder value. The endpoint works only while Hello ShinrAI runs "
            "with the vendor endpoints on."
        )
    else:
        doc["servers"] = [{"url": server_url, "description": f"{VENDOR_NAMES[vendor]} compatibility of ShinrAI"}]
        info["description"] = (
            f"The {VENDOR_NAMES[vendor]} compatibility API of your ShinrAI deployment. "
            "Authenticate with your ShinrAI API key. The vendor contract limits what ShinrAI can return; "
            "the native PII API v2 returns the full results."
        )
    return doc


def strip_query(raw: str, names: set[str]) -> str:
    """The query string without credential parameters; the other pairs keep their encoding."""
    if not raw:
        return ""
    kept = [pair for pair in raw.split("&") if pair and unquote(pair.split("=", 1)[0]).lower() not in names]
    return "&".join(kept)


def bind_loopback(port: int) -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if sys.platform == "win32":
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        else:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", port))
    except OSError:
        sock.close()
        raise
    return sock


class _Listener(uvicorn.Server):
    """A listener inside the app's event loop. The main server keeps the signal handlers."""

    @contextlib.contextmanager
    def capture_signals(self):
        yield


async def reply(send, status: int, body: bytes, headers: list[tuple[str, str]]) -> None:
    head = [(name.encode("latin-1"), value.encode("latin-1", "replace")) for name, value in headers]
    if status in {204, 304}:
        body = b""
    else:
        head.append((b"content-length", str(len(body)).encode()))
    head += [(b"cache-control", b"no-store"), (b"x-content-type-options", b"nosniff")]
    await send({"type": "http.response.start", "status": status, "headers": head})
    await send({"type": "http.response.body", "body": body})


async def refuse(send, vendor: str, exc: Refused) -> None:
    error: dict[str, Any] = {"error": {"code": exc.code, "message": exc.message}}
    headers = [("content-type", "application/json")]
    if vendor == "aws":
        error.update({"__type": exc.code, "message": exc.message})
        headers.append(("x-amzn-errortype", exc.code))
    await reply(send, exc.status, json.dumps(error).encode(), headers)


class VendorSurface:
    """The ASGI app of one vendor listener."""

    def __init__(self, ports: VendorPorts, vendor: str, port: int):
        self.ports, self.vendor, self.port = ports, vendor, port

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return
        headers: dict[str, str] = {}
        for name, value in scope.get("headers", []):
            headers[name.decode("latin-1").lower()] = value.decode("latin-1")
        try:
            rest = self.check(scope, headers)
        except Refused as exc:
            return await refuse(send, self.vendor, exc)
        if rest is None:
            # Not under the prefix of this run: nothing reaches ShinrAI and nothing is traced.
            return await refuse(
                send,
                self.vendor,
                Refused(404, "NotFound", "Not found. Use the endpoint that Hello ShinrAI shows under Connections."),
            )
        method = scope["method"]
        path = unquote(rest)
        if method == "GET" and path == "/openapi.json":
            return await self.openapi(send)
        started = time.perf_counter()
        trace = self.ports.trace(self.vendor, method, path)
        try:
            if method not in ALLOWED_METHODS[self.vendor] or vendor_of_path(path, method, headers) != self.vendor:
                raise Refused(
                    404, "NotFound", f"This endpoint serves the {VENDOR_NAMES[self.vendor]} compatibility API only."
                )
            if self.vendor == "aws" and path != "/":
                raise Refused(404, "NotFound", "The local AWS endpoint accepts Comprehend calls at / only.")
            body = await read_body(receive, headers)
            trace["request"]["bytes"] = len(body)
            status, out_headers, content = await self.ports.forward(
                self.vendor, method, rest, scope.get("query_string", b"").decode("latin-1"), headers, body, trace
            )
        except Refused as exc:
            trace.update(status="error", error=exc.message, http_status=exc.status, elapsed_ms=elapsed(started))
            return await refuse(send, self.vendor, exc)
        except (RemoteError, httpx.HTTPError, ValueError, TypeError) as exc:
            if isinstance(exc, RemoteError):
                message = str(exc)
            elif isinstance(exc, httpx.HTTPError):
                message = "ShinrAI could not be reached. The request was not retried."
            else:
                message = "The request could not be forwarded to ShinrAI."
            trace.update(status="error", error=message, http_status=502, elapsed_ms=elapsed(started))
            if isinstance(exc, RemoteError) and exc.status:
                trace["remote_status"] = exc.status
            return await refuse(send, self.vendor, Refused(502, "BadGateway", message))
        trace.update(
            status="success" if status < 400 else "error",
            http_status=status,
            elapsed_ms=elapsed(started),
            response={"bytes": len(content), "content_type": dict(out_headers).get("content-type", "")},
        )
        await reply(send, status, content, out_headers)

    def check(self, scope, headers: dict[str, str]) -> str | None:
        """The path after the run prefix, or None. Refuses foreign hosts and browser requests."""
        if headers.get("host", "").lower() not in {f"127.0.0.1:{self.port}", f"localhost:{self.port}"}:
            raise Refused(403, "Forbidden", "This endpoint accepts requests for 127.0.0.1 only.")
        # Browsers send Origin with cross-origin writes and Sec-Fetch-Site with every request of a page.
        # Node's fetch sends Sec-Fetch-Mode only, so that header does not identify a browser.
        if "origin" in headers or headers.get("sec-fetch-site", "none") != "none":
            raise Refused(
                403, "Forbidden", "Browser requests are not accepted. Call this endpoint from an SDK or HTTP client."
            )
        raw = (scope.get("raw_path") or scope["path"].encode()).decode("latin-1").split("?", 1)[0]
        rest = self.ports.strip_prefix(raw)
        if rest is None:
            return None
        lowered = rest.lower()
        decoded = unquote(rest)
        if (
            "%2f" in lowered
            or "%5c" in lowered
            or "\\" in decoded
            or any(part in {".", ".."} for part in decoded.split("/"))
        ):
            raise Refused(400, "BadRequest", "Unsupported path.")
        return rest

    async def openapi(self, send) -> None:
        try:
            document = await self.ports.local_openapi(self.vendor)
        except Refused as exc:
            return await refuse(send, self.vendor, exc)
        await reply(send, 200, json.dumps(document).encode(), [("content-type", "application/json")])


async def read_body(receive, headers: dict[str, str]) -> bytes:
    declared = headers.get("content-length", "")
    if declared.isdigit() and int(declared) > MAX_BODY_BYTES:
        raise Refused(413, "RequestTooLarge", "The request body exceeds 20 MB.")
    body = bytearray()
    while True:
        message = await receive()
        if message["type"] == "http.disconnect":
            raise Refused(400, "BadRequest", "The client closed the connection.")
        body += message.get("body", b"")
        if len(body) > MAX_BODY_BYTES:
            raise Refused(413, "RequestTooLarge", "The request body exceeds 20 MB.")
        if not message.get("more_body"):
            return bytes(body)


def elapsed(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)


class VendorPorts:
    """The three listeners of one app run: 127.0.0.1:<base> Azure, <base+1> Google, <base+2> AWS."""

    def __init__(self, app):
        self.app = app
        self.prefix = "/p/" + secrets.token_hex(8)
        self.base_port: int | None = None
        self.last_base_port = DEFAULT_BASE_PORT
        self.listeners: dict[str, tuple[_Listener, asyncio.Task, socket.socket]] = {}
        self.http: httpx.AsyncClient | None = None
        self.lock = asyncio.Lock()

    @property
    def enabled(self) -> bool:
        return bool(self.listeners)

    def port(self, vendor: str) -> int:
        return (self.base_port or self.last_base_port) + VENDORS.index(vendor)

    def endpoint(self, vendor: str) -> str:
        return f"http://127.0.0.1:{self.port(vendor)}{self.prefix}"

    def status(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "base_port": self.base_port or self.last_base_port,
            "endpoints": {vendor: self.endpoint(vendor) for vendor in VENDORS} if self.enabled else None,
        }

    def strip_prefix(self, raw: str) -> str | None:
        head = raw[: len(self.prefix)]
        if not secrets.compare_digest(head.encode("latin-1"), self.prefix.encode("latin-1")):
            return None
        rest = raw[len(self.prefix) :]
        if rest == "":
            return "/"
        return rest if rest.startswith("/") else None

    async def start(self, base_port: int) -> dict[str, Any]:
        async with self.lock:
            if self.enabled and base_port == self.base_port:
                return self.status()
            await self._stop()
            if not 1024 <= base_port <= 65535 - (len(VENDORS) - 1):
                raise VendorPortError("Choose a first port between 1024 and 65533.")
            sockets: list[socket.socket] = []
            try:
                for offset in range(len(VENDORS)):
                    sockets.append(bind_loopback(base_port + offset))
            except OSError:
                for sock in sockets:
                    sock.close()
                raise VendorPortError(
                    f"Port {base_port + len(sockets)} is in use. Choose another first port."
                ) from None
            self.base_port = self.last_base_port = base_port
            if self.app.state.http is None:
                self.http = httpx.AsyncClient()
            for offset, (vendor, sock) in enumerate(zip(VENDORS, sockets, strict=True)):
                config = uvicorn.Config(
                    VendorSurface(self, vendor, base_port + offset),
                    interface="asgi3",
                    lifespan="off",
                    ws="none",
                    access_log=False,
                    log_config=None,
                    log_level="warning",
                    proxy_headers=False,
                    server_header=False,
                    timeout_graceful_shutdown=5,
                )
                listener = _Listener(config)
                task = asyncio.create_task(listener.serve(sockets=[sock]), name=f"hello-shinrai-{vendor}")
                self.listeners[vendor] = (listener, task, sock)
            await self._started()
        return self.status()

    async def _started(self) -> None:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if all(listener.started for listener, _, _ in self.listeners.values()):
                return
            if any(task.done() for _, task, _ in self.listeners.values()):
                break
            await asyncio.sleep(0.02)
        await self._stop()
        raise VendorPortError("The vendor endpoints could not start.")

    async def stop(self) -> dict[str, Any]:
        async with self.lock:
            await self._stop()
        return self.status()

    async def _stop(self) -> None:
        listeners, self.listeners = self.listeners, {}
        for listener, _, _ in listeners.values():
            listener.should_exit = True
        tasks = [task for _, task, _ in listeners.values()]
        if tasks:
            _, pending = await asyncio.wait(tasks, timeout=10)
            for task in pending:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        for _, _, sock in listeners.values():
            with contextlib.suppress(OSError):
                sock.close()
        if self.http is not None:
            await self.http.aclose()
            self.http = None
        self.base_port = None

    def client(self) -> httpx.AsyncClient:
        http = self.app.state.http or self.http
        if http is None:
            raise Refused(503, "ServiceUnavailable", "The vendor endpoints are stopping.")
        return http

    def saved(self) -> tuple[str, str]:
        connection = self.app.state.runtime.connection
        if not connection.shinrai_key:
            raise Refused(
                503,
                "ServiceUnavailable",
                "Hello ShinrAI has no ShinrAI API key. Connect ShinrAI in Hello ShinrAI first.",
            )
        return connection.shinrai_url, connection.shinrai_key

    def trace(self, vendor: str, method: str, path: str) -> dict[str, Any]:
        """A trace without bodies: method, path, status, time, and vendor headers."""
        item: dict[str, Any] = {
            "id": str(uuid.uuid4()),
            "time": int(time.time()),
            "operation": f"vendor.{vendor}",
            "status": "running",
            "endpoint": f"local {VENDOR_NAMES[vendor]} endpoint, port {self.port(vendor)}",
            "request": {"method": method, "path": path},
            "note": "Local vendor endpoint: request and response bodies are not recorded.",
        }
        traces = self.app.state.runtime.traces
        traces.insert(0, item)
        del traces[TRACE_LIMIT:]
        return item

    async def forward(
        self,
        vendor: str,
        method: str,
        rest: str,
        query: str,
        headers: dict[str, str],
        body: bytes,
        trace: dict[str, Any],
    ) -> tuple[int, list[tuple[str, str]], bytes]:
        base, key = self.saved()
        query = strip_query(query, DROPPED_QUERY[vendor])
        if vendor == "aws":
            target = headers.get("x-amz-target", "")
            if not AWS_TARGET.fullmatch(target):
                raise Refused(400, "ValidationException", "Send a valid X-Amz-Target header.")
            if query:
                raise Refused(400, "ValidationException", "The local AWS endpoint accepts no query parameters.")
            trace["request"]["x-amz-target"] = target
            state = self.app.state.runtime
            if not state.aws_credentials:
                state.aws_credentials = await ShinraiClient(base, key, http=self.client()).aws_credentials()
            content_type = headers.get("content-type") or AWS_CONTENT_TYPE
            url, outgoing = aws_signed(base, state.aws_credentials, target, body, content_type)
        else:
            url = f"{base}/v1/{vendor}{rest}" + (f"?{query}" if query else "")
            outgoing = {name: headers[name] for name in FORWARDED_HEADERS[vendor] if name in headers}
            outgoing["Ocp-Apim-Subscription-Key" if vendor == "azure" else "x-goog-api-key"] = key
        trace["destination"] = url
        response = await self.client().request(
            method,
            url,
            content=body if body or method in {"POST", "PUT", "PATCH"} else None,
            headers=outgoing,
            timeout=TIMEOUT_S,
            follow_redirects=False,
        )
        returned, visible = [], {}
        for name, value in response.headers.multi_items():
            lowered = name.lower()
            if lowered not in RETURNED_HEADERS:
                continue
            visible[lowered] = value
            returned.append((lowered, self.local_url(vendor, value) if lowered == "operation-location" else value))
        trace["response_headers"] = visible
        content = response.content
        if vendor == "azure" and "json" in response.headers.get("content-type", "").lower():
            content = self.local_links(vendor, content)
        return response.status_code, returned, content

    def local_url(self, vendor: str, remote: str) -> str:
        """A deployment URL (Operation-Location, nextLink) on this vendor's local endpoint."""
        parts = urlsplit(remote)
        path = parts.path
        match = PREFIXED.match(path)
        if match and match.group(1) == vendor:
            path = match.group(2) or "/"
        return self.endpoint(vendor) + path + (f"?{parts.query}" if parts.query else "")

    def local_links(self, vendor: str, content: bytes) -> bytes:
        try:
            value = json.loads(content)
        except ValueError:
            return content
        changed = False

        def walk(item: Any) -> None:
            nonlocal changed
            if isinstance(item, dict):
                for name, entry in item.items():
                    if name in LINK_KEYS and isinstance(entry, str) and entry.startswith(("http://", "https://", "/")):
                        item[name] = self.local_url(vendor, entry)
                        changed = True
                    else:
                        walk(entry)
            elif isinstance(item, list):
                for entry in item:
                    walk(entry)

        walk(value)
        return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode() if changed else content

    async def local_openapi(self, vendor: str) -> dict[str, Any]:
        base, key = self.saved()
        try:
            schema = await ShinraiClient(base, key, http=self.client()).request("GET", "/openapi.json", timeout=30)
            return vendor_openapi(schema.body, vendor, server_url=self.endpoint(vendor), local=True)
        except RemoteError as exc:
            raise Refused(502, "BadGateway", str(exc)) from None
        except LookupError as exc:
            raise Refused(404, "NotFound", str(exc)) from None
        except TypeError as exc:
            raise Refused(502, "BadGateway", str(exc)) from None
