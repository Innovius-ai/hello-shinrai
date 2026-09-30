from __future__ import annotations

import asyncio
import base64
import json
import re
import time
import uuid
from contextlib import asynccontextmanager
from importlib.resources import files
from pathlib import PurePath
from typing import Any, Literal
from urllib.parse import urlsplit

import httpx
from fastapi import FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import __version__
from .catalog import BY_ID, public_catalog, template_matches
from .client import (
    DOCUMENT_TYPES,
    LANGUAGE_PATTERN,
    PRESETS,
    UPLOAD_TYPES,
    RemoteError,
    Result,
    ShinraiClient,
    aws_signed,
    compact_json,
    known_block,
    mapping_delta,
    merge_pairs,
    render_pdf,
    response_body,
    reversible_pairs,
    v2_options,
    vendor_path,
)
from .exports import collection
from .exports import filename as export_filename
from .restore import Restorer, restore
from .security import sanitize, validate_base_url
from .state import Attachment, RuntimeState
from .vendor_ports import VENDORS, VendorPortError, VendorPorts, issued_aws_pair, vendor_openapi

Vendor = Literal["azure", "google", "aws"]
ExportTarget = Literal["auto", "local", "deployment"]


class ShinraiSettings(BaseModel):
    base_url: str = "https://api.shinrai.innovius.io"
    api_key: str = ""
    remember: bool = False


class LlmSettings(BaseModel):
    provider: Literal["innovius", "openai", "kie", "custom"] = "innovius"
    models_url: str
    inference_url: str
    protocol: Literal["chat", "responses"] = "chat"
    api_key: str = ""
    model: str = ""
    remember: bool = False


# Modes whose replacements can be restored: pseudonyms (replace) and per-value labels.
RESTORABLE_MODES = {"replace", "label"}


class TextRun(BaseModel):
    operation: Literal["analyze", "redact", "batch", "restore", "restore-remote"] = "redact"
    text: str = Field(min_length=1, max_length=200_000)
    mode: Literal["replace", "mask", "label"] = "replace"
    model: str = "latest"
    tier: Literal["standard", "batch", "realtime"] = "standard"
    threshold: float | None = Field(default=None, ge=0, le=1)
    language: str = Field(default="auto", pattern=LANGUAGE_PATTERN)


class DiscoverModels(BaseModel):
    models_url: str | None = None
    api_key: str = ""


class ChatRun(BaseModel):
    text: str = Field(min_length=1, max_length=200_000)
    system: str = Field(default="You are a helpful assistant.", max_length=50_000)
    attachment_ids: list[str] = Field(default_factory=list, max_length=8)
    include_visuals: bool = False
    vision_confirmed: bool = False
    stream: bool = True
    mode: Literal["replace", "mask", "label"] = "replace"
    shinrai_model: str = "latest"
    tier: Literal["standard", "batch", "realtime"] = "realtime"
    threshold: float | None = Field(default=None, ge=0, le=1)
    language: str = Field(default="auto", pattern=LANGUAGE_PATTERN)


class AzureSettings(BaseModel):
    endpoint: str = ""
    api_key: str = ""
    remember: bool = False


class AzureCompare(BaseModel):
    path: str = "/language/:analyze-text"
    api_version: str = "2026-05-01"
    body: dict[str, Any]
    include_real_azure: bool = False


class VendorPortsRun(BaseModel):
    enabled: bool
    base_port: int | None = Field(default=None, ge=1024, le=65533)


class ExplorerRun(BaseModel):
    operation_id: str
    method: Literal["GET", "POST", "PUT", "PATCH", "DELETE"] | None = None
    path: str | None = None
    query: dict[str, str | int | float | bool] = Field(default_factory=dict)
    body: Any = None
    download: bool = False


def keyring_module():
    from . import credentials

    return credentials


@asynccontextmanager
async def lifespan(app: FastAPI):
    credentials = keyring_module()
    state: RuntimeState = app.state.runtime
    if not state.connection.shinrai_key:
        state.connection.shinrai_key = credentials.read("shinrai")
    if not state.connection.llm_key:
        state.connection.llm_key = credentials.read("llm")
    if not state.connection.azure_key:
        state.connection.azure_key = credentials.read("azure")
    ports: VendorPorts = app.state.vendor_ports
    if app.state.vendor_ports_autostart is not None:
        await ports.start(app.state.vendor_ports_autostart)
    try:
        yield
    finally:
        await ports.stop()


def create_app(*, http: httpx.AsyncClient | None = None, vendor_ports: int | None = None) -> FastAPI:
    """The local workbench. `vendor_ports` starts the vendor endpoints at that first port."""
    app = FastAPI(title="Hello ShinrAI", version=__version__, docs_url=None, redoc_url=None, lifespan=lifespan)
    app.state.runtime = RuntimeState()
    app.state.http = http
    app.state.vendor_ports = VendorPorts(app)
    app.state.vendor_ports_autostart = vendor_ports
    static = files("hello_shinrai").joinpath("static")
    app.mount("/assets", StaticFiles(directory=str(static)), name="assets")

    @app.middleware("http")
    async def local_guard(request: Request, call_next):
        host = (request.url.hostname or "").lower()
        if host not in {"127.0.0.1", "localhost", "::1", "testserver"}:
            return JSONResponse({"detail": "Hello ShinrAI accepts local browser requests only."}, status_code=403)
        if request.url.path.startswith("/api/"):
            if request.headers.get("x-hello-shinrai-session") != app.state.runtime.token:
                return JSONResponse(
                    {"detail": "The local session token is missing or expired. Reload the page."}, status_code=403
                )
            origin = request.headers.get("origin")
            if origin:
                parsed = urlsplit(origin)
                if parsed.hostname not in {"127.0.0.1", "localhost", "::1", "testserver"}:
                    return JSONResponse({"detail": "Cross-origin requests are blocked."}, status_code=403)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; img-src 'self' data: blob:; style-src 'self'; script-src 'self'; connect-src 'self'"
        )
        return response

    @app.get("/", response_class=HTMLResponse)
    async def index():
        html = static.joinpath("index.html").read_text(encoding="utf-8")
        return HTMLResponse(html.replace("__SESSION_TOKEN__", app.state.runtime.token))

    @app.get("/api/bootstrap")
    async def bootstrap():
        state: RuntimeState = app.state.runtime
        credentials = keyring_module()
        connection = state.connection
        return {
            "version": __version__,
            "started_at": state.started_at,
            "shinrai": {"base_url": connection.shinrai_url, "has_key": bool(connection.shinrai_key)},
            "llm": {
                "provider": connection.llm_provider,
                "models_url": connection.llm_models_url,
                "inference_url": connection.llm_inference_url,
                "protocol": connection.llm_protocol,
                "model": connection.llm_model,
                "has_key": bool(connection.llm_key),
                "model_count": state.llm_model_count,
            },
            "azure": {"endpoint": connection.azure_url, "has_key": bool(connection.azure_key)},
            "secure_storage": credentials.available(),
            "vendor_ports": app.state.vendor_ports.status(),
            "capabilities": state.capabilities,
            "usage": state.usage,
            "attachments": attachment_views(state),
        }

    @app.post("/api/settings/shinrai")
    async def connect_shinrai(body: ShinraiSettings):
        state: RuntimeState = app.state.runtime
        try:
            url = validate_base_url(body.base_url)
            key = body.api_key or state.connection.shinrai_key
            client = ShinraiClient(url, key, http=app.state.http)
            capabilities, usage, routes = await client.connect()
        except (ValueError, RemoteError) as exc:
            trace_error(state, "shinrai.connect", exc, destination=body.base_url)
            raise api_error(exc)
        if (url, key) != (state.connection.shinrai_url, state.connection.shinrai_key):
            # The issued AWS pair belongs to the previous key and deployment.
            state.aws_credentials = state.aws_credentials_owner = None
        state.connection.shinrai_url, state.connection.shinrai_key = url, key
        state.capabilities, state.usage, state.discovered_routes = capabilities.body, usage.body, routes
        saved = keyring_module().write("shinrai", key) if body.remember else False
        state.traces.insert(
            0,
            make_trace(
                "shinrai.connect",
                destination=[url + path for path in ("/v2/capabilities", "/v2/usage")],
                status="success",
                elapsed_ms=capabilities.elapsed_ms + usage.elapsed_ms,
                response={"capabilities": capabilities.body, "usage": usage.body},
                response_headers={**capabilities.headers, **usage.headers},
            ),
        )
        return {
            "capabilities": capabilities.body,
            "usage": usage.body,
            "securely_saved": saved,
            "routes_discovered": len(routes),
        }

    @app.post("/api/settings/llm")
    async def save_llm(body: LlmSettings):
        state: RuntimeState = app.state.runtime
        try:
            models_url = validate_base_url(body.models_url, allow_path=True)
            inference_url = validate_base_url(body.inference_url, allow_path=True)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        connection = state.connection
        key = body.api_key or connection.llm_key
        if connection.llm_models_url != models_url:
            state.llm_model_count = None
        connection.llm_provider = body.provider
        connection.llm_models_url = models_url
        connection.llm_inference_url = inference_url
        connection.llm_protocol = body.protocol
        connection.llm_key = key
        connection.llm_model = body.model.strip()
        saved = keyring_module().write("llm", key) if body.remember and key else False
        return {"saved": True, "securely_saved": saved, "has_key": bool(key), "model": connection.llm_model}

    @app.post("/api/settings/azure")
    async def save_azure(body: AzureSettings):
        state: RuntimeState = app.state.runtime
        endpoint = validate_base_url(body.endpoint) if body.endpoint else ""
        key = body.api_key or state.connection.azure_key
        state.connection.azure_url, state.connection.azure_key = endpoint, key
        saved = keyring_module().write("azure", key) if body.remember and key else False
        return {"saved": True, "securely_saved": saved, "has_key": bool(key), "endpoint": endpoint}

    @app.delete("/api/settings/secrets")
    async def forget_secrets():
        state: RuntimeState = app.state.runtime
        keyring_module().forget()
        state.connection.shinrai_key = ""
        state.connection.llm_key = ""
        state.connection.azure_key = ""
        state.aws_credentials = state.aws_credentials_owner = None
        state.llm_model_count = None
        return {"forgotten": True}

    @app.post("/api/models/discover")
    async def discover_models(body: DiscoverModels):
        state: RuntimeState = app.state.runtime
        destination = body.models_url or state.connection.llm_models_url
        try:
            url = validate_base_url(destination, allow_path=True)
            key = body.api_key or state.connection.llm_key
            started = time.perf_counter()
            response = await request_url(app, "GET", url, key=key, timeout=30)
            models = parse_models(response.body)
        except (RemoteError, ValueError) as exc:
            trace_error(state, "llm.models", exc, destination=destination)
            raise api_error(exc)
        trace = make_trace(
            "llm.models", destination=url, status="success", elapsed_ms=elapsed(started), response=response.body
        )
        state.traces.insert(0, trace)
        state.llm_model_count = len(models)
        return {"models": models, "trace": sanitize(trace, include_sensitive=True)}

    @app.post("/api/text")
    async def run_text(body: TextRun):
        state: RuntimeState = app.state.runtime
        if body.operation == "restore":
            return restore_text_locally(state, body.text)
        client = configured_shinrai(app)
        try:
            path, request_body = text_request(state, body)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        trace = make_trace(
            "text." + body.operation,
            destination=state.connection.shinrai_url + path,
            request=request_body,
            status="running",
        )
        state.traces.insert(0, trace)
        try:
            result = await client.request("POST", path, json=request_body)
            complete_trace(trace, result)
        except RemoteError as exc:
            fail_trace(trace, exc)
            raise api_error(exc)
        if body.operation in {"redact", "batch"}:
            returned = mapping_delta(result.body)
            state.text_mapping = reversible_pairs(returned) if body.mode in RESTORABLE_MODES else {}
        if body.operation == "restore-remote":
            rows = result.body.get("results") if isinstance(result.body, dict) else None
            trace["response"] = {
                "restored_text": [row.get("text") for row in rows or [] if isinstance(row, dict)],
                "restored": result.body.get("restored") if isinstance(result.body, dict) else None,
            }
        return {"result": result.body, "trace": sanitize(trace, include_sensitive=True)}

    @app.post("/api/files")
    async def upload_file(
        file: UploadFile = File(...),  # noqa: B008 - FastAPI dependency marker
        mode: Literal["replace", "mask", "label"] = Form("replace"),
        language: str = Form("auto"),
    ):
        state: RuntimeState = app.state.runtime
        content = await file.read(10_000_001)
        media_type = upload_media_type(file.content_type, file.filename)
        if not re.fullmatch(LANGUAGE_PATTERN, language):
            raise HTTPException(422, "Enter a BCP 47 language tag such as en or de, or auto.")
        route = "/v2/uploads -> /v2/jobs (document)" if media_type in DOCUMENT_TYPES else "/v2/protect"
        trace = make_trace(
            "file.protect",
            destination=state.connection.shinrai_url + route,
            request={
                "content_type": media_type,
                "bytes": len(content),
                "mode": mode,
                "language": language,
                "original_filename": file.filename,
            },
            status="running",
        )
        state.traces.insert(0, trace)
        try:
            client = configured_shinrai(app)
            require_document_jobs(state, media_type)
            result = await client.protect_file_v2(content, media_type, mode=mode, language=language)
        except HTTPException as exc:
            fail_trace(trace, exc)
            raise
        except (ValueError, RemoteError) as exc:
            fail_trace(trace, exc)
            raise api_error(exc)
        pages, preview_note = page_previews(result)
        note = " ".join(item for item in (result.get("note", ""), preview_note) if item)
        identifier = str(uuid.uuid4())
        attachment = Attachment(
            id=identifier,
            name=file.filename or "attachment",
            media_type=media_type,
            protected_text=result["text"],
            mapping=result["mapping"],
            protected_pdf=result["pdf"],
            page_images=pages,
            job_id=result["job_id"],
            cleanup=result["cleanup"],
            trace_id=trace["id"],
            protected_image=result.get("image", b""),
            entities=result.get("entities"),
            note=note,
        )
        state.attachments[identifier] = attachment
        if result.get("request") is not None:
            trace["shinrai_request"] = result["request"]
        trace.update(
            status="success",
            elapsed_ms=result["elapsed_ms"],
            response={
                "job": result["job"],
                **({"shinrai_response": result["response"]} if "response" in result else {}),
                "protected_text": result["text"],
                "mapping": result["mapping"],
                "pages_rendered": len(pages),
                **({"note": note} if note else {}),
                "cleanup": result["cleanup"],
            },
            response_headers=result["headers"],
        )
        return {"attachment": attachment_view(attachment), "trace": sanitize(trace, include_sensitive=True)}

    @app.get("/api/attachments/{identifier}/preview/{page}")
    async def attachment_preview(identifier: str, page: int):
        attachment = get_attachment(app.state.runtime, identifier)
        if page < 1 or page > len(attachment.page_images):
            raise HTTPException(404, "Page was not found.")
        return Response(attachment.page_images[page - 1], media_type="image/png")

    @app.get("/api/attachments/{identifier}/download/{kind}")
    async def attachment_download(identifier: str, kind: Literal["text", "pdf", "image", "mapping"]):
        attachment = get_attachment(app.state.runtime, identifier)
        if kind == "text":
            return Response(
                attachment.protected_text,
                media_type="text/plain",
                headers={"Content-Disposition": "attachment; filename=protected.txt"},
            )
        if kind == "pdf":
            if not attachment.protected_pdf:
                raise HTTPException(404, "This attachment has no redacted PDF.")
            return Response(
                attachment.protected_pdf,
                media_type="application/pdf",
                headers={"Content-Disposition": "attachment; filename=protected.pdf"},
            )
        if kind == "image":
            if not attachment.protected_image:
                raise HTTPException(404, "This attachment has no redacted image.")
            return Response(
                attachment.protected_image,
                media_type="image/png",
                headers={"Content-Disposition": "attachment; filename=protected.png"},
            )
        # The map in the shape POST /v2/restore accepts.
        mapping = {"mapping": {"known": known_block(attachment.mapping)["known"]}}
        return JSONResponse(mapping, headers={"Content-Disposition": "attachment; filename=protected.json"})

    @app.delete("/api/attachments/{identifier}")
    async def delete_attachment(identifier: str):
        if app.state.runtime.attachments.pop(identifier, None) is None:
            raise HTTPException(404, "Attachment was not found.")
        return {"deleted": True}

    @app.post("/api/chat")
    async def chat(body: ChatRun):
        return StreamingResponse(run_chat(app, body), media_type="application/x-ndjson")

    @app.delete("/api/chat")
    async def clear_chat():
        from .state import Chat

        app.state.runtime.chat = Chat()
        return {"cleared": True}

    @app.post("/api/azure/compare")
    async def compare_azure(body: AzureCompare):
        state: RuntimeState = app.state.runtime
        if body.path not in {"/language/:analyze-text", "/text/analytics/v3.1/entities/recognition/pii"}:
            raise HTTPException(422, "Choose a supported synchronous Azure comparison path.")
        query = {"api-version": body.api_version} if body.path.startswith("/language/") else {}
        calls = [
            call_azure(
                app,
                state.connection.shinrai_url,
                state.connection.shinrai_key,
                vendor_path(body.path),
                query,
                body.body,
                "ShinrAI",
            )
        ]
        if body.include_real_azure:
            if not state.connection.azure_url or not state.connection.azure_key:
                raise HTTPException(422, "Save the optional real Azure endpoint and key first.")
            calls.append(
                call_azure(
                    app, state.connection.azure_url, state.connection.azure_key, body.path, query, body.body, "Azure"
                )
            )
        results = await asyncio.gather(*calls)
        trace = make_trace(
            "azure.compare",
            destination=[item["destination"] for item in results],
            request=body.body,
            response=results,
            status="success",
            elapsed_ms=max(item["elapsed_ms"] for item in results),
        )
        state.traces.insert(0, trace)
        return {"results": results, "trace": sanitize(trace, include_sensitive=True)}

    @app.get("/api/explorer/catalog")
    async def explorer_catalog():
        state: RuntimeState = app.state.runtime
        return {
            "operations": public_catalog(state.discovered_routes),
            "discovered_routes": [{"method": method, "path": path} for method, path in sorted(state.discovered_routes)],
        }

    @app.post("/api/explorer/run")
    async def explorer_run(body: ExplorerRun):
        state: RuntimeState = app.state.runtime
        operation = BY_ID.get(body.operation_id)
        if operation:
            method, path = operation["method"], operation["path"]
            if body.path:
                if not template_matches(path, body.path):
                    raise HTTPException(403, "The edited path does not match this reviewed operation.")
                path = body.path
            query = {**operation.get("query", {}), **body.query}
            payload = operation.get("body") if body.body is None else body.body
            auth = operation["auth"]
        elif body.operation_id == "discovered" and body.method and body.path:
            method, path, query, payload, auth = body.method, body.path, body.query, body.body, auth_for_path(body.path)
            if not any(
                route_method == method and template_matches(route, path)
                for route_method, route in state.discovered_routes
            ):
                raise HTTPException(403, "That route was not present in the deployment's safe OpenAPI discovery.")
        else:
            raise HTTPException(404, "Explorer operation was not found.")
        trace = make_trace(
            "explorer." + body.operation_id,
            destination=state.connection.shinrai_url + vendor_path(path),
            request={"query": query, "body": payload},
            status="running",
        )
        state.traces.insert(0, trace)
        try:
            result = await explorer_request(app, operation, method, path, query, payload, auth)
            complete_trace(trace, result)
        except (RemoteError, TypeError, ValueError) as exc:
            fail_trace(trace, exc)
            raise api_error(exc)
        if isinstance(result.body, bytes) and body.download:
            return Response(
                result.body,
                media_type=result.headers.get("content-type", "application/octet-stream"),
                headers={"Content-Disposition": "attachment; filename=shinrai-artifact.bin"},
            )
        return {
            "result": sanitize(result.body, include_sensitive=True),
            "headers": result.headers,
            "trace": sanitize(trace, include_sensitive=True),
        }

    @app.get("/api/vendor-ports")
    async def vendor_ports_status():
        return app.state.vendor_ports.status()

    @app.post("/api/vendor-ports")
    async def set_vendor_ports(body: VendorPortsRun):
        ports: VendorPorts = app.state.vendor_ports
        try:
            if body.enabled:
                status = await ports.start(body.base_port or ports.status()["base_port"])
            else:
                status = await ports.stop()
        except VendorPortError as exc:
            trace_error(app.state.runtime, "vendor.endpoints", exc)
            raise HTTPException(409, str(exc)) from exc
        first = status["base_port"]
        app.state.runtime.traces.insert(
            0,
            make_trace(
                "vendor.endpoints",
                status="success",
                destination=f"127.0.0.1 ports {first}-{first + len(VENDORS) - 1}",
                response={"enabled": status["enabled"]},
            ),
        )
        return status

    @app.get("/api/exports/openapi/{vendor}")
    async def export_openapi(vendor: Vendor, target: ExportTarget = "auto"):
        local = export_target(app, target)
        client = configured_shinrai(app)
        server = app.state.vendor_ports.endpoint(vendor) if local else f"{client.base}/v1/{vendor}"
        try:
            schema = await client.request("GET", "/openapi.json", timeout=30)
        except RemoteError as exc:
            raise api_error(exc) from exc
        try:
            document = vendor_openapi(schema.body, vendor, server_url=server, local=local)
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from exc
        except TypeError as exc:
            raise HTTPException(502, str(exc)) from exc
        suffix = "local" if local else "deployment"
        return JSONResponse(
            document, headers={"Content-Disposition": f"attachment; filename=shinrai-{vendor}-{suffix}.openapi.json"}
        )

    @app.get("/api/exports/collection/{vendor}")
    async def export_collection(
        vendor: Vendor,
        fmt: Literal["http", "curl"] = Query("http", alias="format"),
        target: ExportTarget = "auto",
    ):
        local = export_target(app, target)
        text = collection(
            vendor,
            fmt=fmt,
            shinrai_url=app.state.runtime.connection.shinrai_url,
            local_endpoint=app.state.vendor_ports.endpoint(vendor) if local else None,
            version=__version__,
        )
        name = export_filename(vendor, fmt, local)
        return Response(
            text,
            media_type="text/plain; charset=utf-8",
            headers={"Content-Disposition": f"attachment; filename={name}"},
        )

    @app.get("/api/traces")
    async def traces():
        return {"traces": [sanitize(item, include_sensitive=True) for item in app.state.runtime.traces]}

    @app.get("/api/traces/export")
    async def export_traces(include_sensitive: bool = False):
        payload = json.dumps(
            {
                "schema": 1,
                "created_at": int(time.time()),
                "hello_shinrai_version": __version__,
                "includes_sensitive_data": include_sensitive,
                "traces": [sanitize(t, include_sensitive=include_sensitive) for t in app.state.runtime.traces],
            },
            ensure_ascii=False,
            indent=2,
        )
        return Response(
            payload,
            media_type="application/json",
            headers={"Content-Disposition": "attachment; filename=hello-shinrai-diagnostics.json"},
        )

    @app.delete("/api/traces")
    async def clear_traces():
        app.state.runtime.traces.clear()
        return {"cleared": True}

    return app


def export_target(app: FastAPI, target: str) -> bool:
    """True for the local vendor endpoints; `auto` picks them while they run."""
    running = app.state.vendor_ports.enabled
    if target == "local" and not running:
        raise HTTPException(409, "Start the vendor endpoints first, or export for the deployment.")
    return running if target == "auto" else target == "local"


def text_request(state: RuntimeState, body: TextRun) -> tuple[str, dict[str, Any]]:
    """The Text workspace request: /v2/detect, /v2/protect or /v2/restore."""
    if body.operation == "restore-remote":
        if not state.text_mapping:
            raise ValueError("Protect a text with Replace or Label first. No restorable replacement map is loaded.")
        known = known_block(state.text_mapping)["known"]
        return "/v2/restore", {"mapping": {"known": known}, "inputs": [{"id": "1", "text": body.text}]}
    texts = [part.strip() for part in body.text.split("\n---\n")] if body.operation == "batch" else [body.text]
    if not all(texts):
        raise ValueError("Every batch text needs content. Remove empty sections between --- lines.")
    options = v2_options(model=body.model, tier=body.tier, threshold=body.threshold, language=body.language)
    if body.operation == "analyze":
        return "/v2/detect", {"text": body.text, **options, "output": {"include": ["entities"]}}
    inputs = {"text": texts[0]} if body.operation == "redact" else {"texts": texts}
    return "/v2/protect", {
        **inputs,
        **options,
        "policy": {"preset": PRESETS[body.mode]},
        "output": {"include": ["entities", "mapping"]},
    }


def restore_text_locally(state: RuntimeState, text: str) -> dict[str, Any]:
    """Restore with the last Text protection map in this process; nothing leaves the computer."""
    if not state.text_mapping:
        raise HTTPException(422, "Protect a text with Replace or Label first. No restorable replacement map is loaded.")
    started = time.perf_counter()
    try:
        restored = restore(text, state.text_mapping)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    trace = make_trace(
        "text.restore",
        destination="local restore (no network request)",
        request={"text": text, "pairs": len(state.text_mapping)},
        response={"restored_text": restored},
        status="success",
        elapsed_ms=elapsed(started),
    )
    state.traces.insert(0, trace)
    result = {"results": [{"input_id": "1", "text": restored}], "restored_locally": True}
    return {"result": result, "trace": sanitize(trace, include_sensitive=True)}


async def run_chat(app: FastAPI, body: ChatRun):
    state: RuntimeState = app.state.runtime
    connection = state.connection
    trace = make_trace(
        "chat",
        destination=connection.llm_inference_url,
        status="protecting",
        request={
            "original_input": body.text,
            "system": body.system,
            "attachment_ids": body.attachment_ids,
            "include_visuals": body.include_visuals,
            "model": connection.llm_model,
            "protocol": connection.llm_protocol,
        },
    )
    state.traces.insert(0, trace)

    def event(kind: str, **data: Any) -> bytes:
        return (
            json.dumps({"type": kind, **sanitize(data, include_sensitive=True)}, ensure_ascii=False) + "\n"
        ).encode()

    try:
        if not connection.llm_key or not connection.llm_model:
            raise ValueError("Save an LLM API key and model before chatting.")
        attachments = [get_attachment(state, identifier) for identifier in body.attachment_ids]
        if body.include_visuals and not body.vision_confirmed:
            raise ValueError("Confirm that the selected model accepts image input before sending protected visuals.")
        originals: list[str] = []
        positions: list[tuple[str, int]] = []
        if body.system:
            positions.append(("system", len(originals)))
            originals.append(body.system)
        for index, message in enumerate(state.chat.messages):
            positions.append((f"history:{index}", len(originals)))
            originals.append(message["content"])
        positions.append(("user", len(originals)))
        originals.append(body.text)
        for index, attachment in enumerate(attachments):
            positions.append((f"attachment:{index}", len(originals)))
            originals.append(
                restore(attachment.protected_text, attachment.mapping)
                if attachment.mapping
                else attachment.protected_text
            )
        # One POST /v2/protect for the whole conversation. Earlier pairs go in mapping.known,
        # so a person keeps one surrogate for the conversation (v2 draws new ones per request).
        keeps_map = body.mode in RESTORABLE_MODES
        sent = [index for index, text in enumerate(originals) if text]
        protected, request_body = await configured_shinrai(app).protect_texts(
            [originals[index] for index in sent],
            mode=body.mode,
            model=body.shinrai_model,
            tier=body.tier,
            threshold=body.threshold,
            language=body.language,
            known=state.chat.mapping if keeps_map else None,
        )
        rows = protected.body.get("results") if isinstance(protected.body, dict) else None
        by_id = {str(row.get("input_id")): row for row in rows or [] if isinstance(row, dict)}
        values = [""] * len(originals)
        for number, index in enumerate(sent, start=1):
            row = by_id.get(str(number))
            if not row or row.get("status", "ok") != "ok" or not isinstance(row.get("output"), str):
                raise RemoteError("ShinrAI returned an incomplete protection result.", body=protected.body)
            values[index] = row["output"]
        if keeps_map:
            state.chat.mapping = merge_pairs(state.chat.mapping, mapping_delta(protected.body))
        else:
            state.chat.mapping.clear()
        protected_by_name = {name: values[index] for name, index in positions}
        messages = []
        if body.system:
            messages.append({"role": "system", "content": protected_by_name["system"]})
        for index, message in enumerate(state.chat.messages):
            messages.append({"role": message["role"], "content": protected_by_name[f"history:{index}"]})
        attachment_text = "\n\n".join(
            f"Protected attachment {index + 1}:\n{protected_by_name[f'attachment:{index}']}"
            for index in range(len(attachments))
        )
        user_text = protected_by_name["user"] + (("\n\n" + attachment_text) if attachment_text else "")
        outgoing = llm_payload(
            connection, messages, user_text, attachments if body.include_visuals else [], stream=body.stream
        )
        trace.update(
            status="calling model",
            shinrai={
                "endpoint": "/v2/protect",
                "request": {
                    **{key: value for key, value in request_body.items() if key != "inputs"},
                    "original_inputs": [item["text"] for item in request_body["inputs"]],
                },
                "response": protected.body,
                "elapsed_ms": protected.elapsed_ms,
                "headers": protected.headers,
            },
            llm_request=outgoing,
        )
        yield event("protected", protected_text=user_text, shinrai=trace["shinrai"], outgoing=outgoing)
        started = time.perf_counter()
        headers = {"Authorization": "Bearer " + connection.llm_key, "Content-Type": "application/json"}
        async with http_client(app) as client:
            if body.stream:
                async with client.stream(
                    "POST",
                    connection.llm_inference_url,
                    headers=headers,
                    json=outgoing,
                    timeout=300,
                    follow_redirects=False,
                ) as response:
                    if response.status_code >= 300:
                        raw = await response.aread()
                        raise RemoteError(
                            f"The model provider rejected the protected request (HTTP {response.status_code}).",
                            status=response.status_code,
                            body=raw.decode(errors="replace")[:4000],
                        )
                    restorer = Restorer(state.chat.mapping if keeps_map else {})
                    provider_text, restored_text, first_token_ms = "", "", None
                    async for line in response.aiter_lines():
                        delta = stream_delta(line, connection.llm_protocol)
                        if not delta:
                            continue
                        if first_token_ms is None:
                            first_token_ms = elapsed(started)
                        provider_text += delta
                        restored_delta = restorer.feed(delta)
                        restored_text += restored_delta
                        yield event("delta", provider=delta, restored=restored_delta, first_token_ms=first_token_ms)
                    tail = restorer.finish()
                    restored_text += tail
                    if tail:
                        yield event("delta", provider="", restored=tail, first_token_ms=first_token_ms)
                    provider_body: Any = {"streamed_text": provider_text}
            else:
                response = await client.post(
                    connection.llm_inference_url, headers=headers, json=outgoing, timeout=300, follow_redirects=False
                )
                provider_body = response_body(response)
                if response.status_code >= 300:
                    raise RemoteError(
                        f"The model provider rejected the protected request (HTTP {response.status_code}).",
                        status=response.status_code,
                        body=provider_body,
                    )
                provider_text = response_text(provider_body, connection.llm_protocol)
                restored_text = restore(provider_text, state.chat.mapping) if keeps_map else provider_text
                first_token_ms = None
                yield event("delta", provider=provider_text, restored=restored_text)
        total_ms = elapsed(started)
        state.chat.messages.extend(
            [{"role": "user", "content": body.text}, {"role": "assistant", "content": restored_text}]
        )
        trace.update(
            status="success",
            elapsed_ms=protected.elapsed_ms + total_ms,
            model_elapsed_ms=total_ms,
            first_token_ms=first_token_ms,
            provider_response=provider_body,
            restored_response=restored_text,
        )
        yield event(
            "complete",
            trace=trace,
            provider_reply=provider_text,
            restored_reply=restored_text,
            total_ms=total_ms,
            first_token_ms=first_token_ms,
        )
    except asyncio.CancelledError:
        trace.update(status="cancelled", error="The browser stopped the request; the upstream connection was closed.")
        raise
    except (ValueError, RemoteError, httpx.HTTPError, HTTPException, KeyError, TypeError) as exc:
        fail_trace(trace, exc)
        yield event("error", message=str(exc), trace=trace)


def llm_payload(
    connection, prior: list[dict[str, str]], user_text: str, attachments: list[Attachment], *, stream: bool
) -> dict[str, Any]:
    images = [image for attachment in attachments for image in attachment.page_images]
    if connection.llm_protocol == "chat":
        content: Any = user_text
        if images:
            content = [{"type": "text", "text": user_text}] + [
                {"type": "image_url", "image_url": {"url": "data:image/png;base64," + base64.b64encode(image).decode()}}
                for image in images
            ]
        payload: dict[str, Any] = {
            "model": connection.llm_model,
            "messages": [*prior, {"role": "user", "content": content}],
            "stream": stream,
        }
        if stream:
            payload["stream_options"] = {"include_usage": True}
        if connection.llm_provider in {"innovius", "openai"}:
            payload["store"] = False
        return payload
    content = [{"type": "input_text", "text": user_text}] + [
        {"type": "input_image", "image_url": "data:image/png;base64," + base64.b64encode(image).decode()}
        for image in images
    ]
    inputs = [
        {"role": message["role"], "content": [{"type": "input_text", "text": message["content"]}]} for message in prior
    ]
    inputs.append({"role": "user", "content": content})
    return {"model": connection.llm_model, "input": inputs, "stream": stream, "store": False}


def stream_delta(line: str, protocol: str) -> str:
    if not line.startswith("data:"):
        return ""
    raw = line[5:].strip()
    if not raw or raw == "[DONE]":
        return ""
    try:
        value = json.loads(raw)
    except ValueError:
        return ""
    if protocol == "chat":
        choices = value.get("choices") or []
        if choices and isinstance(choices[0].get("delta", {}).get("content"), str):
            return choices[0]["delta"]["content"]
        return ""
    return value.get("delta", "") if value.get("type") == "response.output_text.delta" else ""


def response_text(body: Any, protocol: str) -> str:
    if not isinstance(body, dict):
        raise RemoteError("The model provider returned an invalid response.")
    if protocol == "chat":
        try:
            text = body["choices"][0]["message"]["content"]
            if not isinstance(text, str):
                raise TypeError
            return text
        except (KeyError, IndexError, TypeError):
            raise RemoteError("The model provider returned no assistant text.") from None
    if isinstance(body.get("output_text"), str):
        return body["output_text"]
    pieces = [
        part.get("text", "")
        for item in body.get("output", [])
        if isinstance(item, dict)
        for part in item.get("content", [])
        if isinstance(part, dict) and part.get("type") == "output_text"
    ]
    if not pieces:
        raise RemoteError("The model provider returned no output text.")
    return "".join(pieces)


async def explorer_request(app, operation, method, path, query, payload, auth) -> Result:
    state: RuntimeState = app.state.runtime
    if auth == "aws":
        if not operation:
            raise ValueError("AWS signing requires a catalogued operation.")
        return await aws_request(app, operation, payload)
    headers = {}
    if auth == "azure":
        headers["Ocp-Apim-Subscription-Key"] = state.connection.shinrai_key
    elif auth == "google":
        headers["x-goog-api-key"] = state.connection.shinrai_key
    client = configured_shinrai(app)
    return await client.request(
        method,
        path,
        params=query,
        json=payload if payload is not None else None,
        headers=headers,
        bearer_auth=auth == "bearer",
    )


async def aws_request(app, operation, payload) -> Result:
    client = configured_shinrai(app)
    # The base URL and the pair belong to the connection of this call, also if a new key connects meanwhile.
    pair = await issued_aws_pair(app.state.runtime, client)
    body = compact_json(payload)
    url, headers = aws_signed(client.base, pair, operation["aws_target"], body)
    started = time.perf_counter()
    async with http_client(app) as http:
        response = await http.post(url, content=body, headers=headers, timeout=300, follow_redirects=False)
    result = Result(
        response.status_code,
        response_body(response),
        {
            k.lower(): v
            for k, v in response.headers.items()
            if k.lower() in {"content-type", "x-amzn-requestid", "x-amzn-errortype"}
        },
        elapsed(started),
    )
    if response.status_code >= 300:
        raise RemoteError(
            f"ShinrAI rejected the AWS request (HTTP {response.status_code}).",
            status=response.status_code,
            body=result.body,
        )
    return result


async def call_azure(app, endpoint, key, path, query, body, label):
    url = endpoint.rstrip("/") + path
    started = time.perf_counter()
    try:
        async with http_client(app) as client:
            response = await client.post(
                url,
                params=query,
                json=body,
                headers={"Ocp-Apim-Subscription-Key": key},
                timeout=300,
                follow_redirects=False,
            )
        return {
            "label": label,
            "destination": url,
            "status": response.status_code,
            "elapsed_ms": elapsed(started),
            "headers": {
                k.lower(): v
                for k, v in response.headers.items()
                if k.lower() in {"x-request-id", "apim-request-id", "x-records-charged", "x-shinrai-warnings"}
            },
            "body": response_body(response),
        }
    except httpx.HTTPError as exc:
        return {"label": label, "destination": url, "status": 0, "elapsed_ms": elapsed(started), "error": str(exc)}


class _ClientContext:
    def __init__(self, existing):
        self.existing = existing
        self.created = None

    async def __aenter__(self):
        if self.existing:
            return self.existing
        self.created = httpx.AsyncClient()
        return self.created

    async def __aexit__(self, *args):
        if self.created:
            await self.created.aclose()


def http_client(app):
    return _ClientContext(app.state.http)


async def request_url(app, method, url, *, key="", timeout=300) -> Result:
    headers = {"Authorization": "Bearer " + key} if key else {}
    started = time.perf_counter()
    try:
        async with http_client(app) as client:
            response = await client.request(method, url, headers=headers, timeout=timeout, follow_redirects=False)
    except httpx.HTTPError as exc:
        raise RemoteError("The endpoint could not be reached.") from exc
    body = response_body(response)
    if response.status_code >= 300:
        raise RemoteError(
            f"The endpoint rejected the request (HTTP {response.status_code}).", status=response.status_code, body=body
        )
    return Result(response.status_code, body, {}, elapsed(started))


def configured_shinrai(app) -> ShinraiClient:
    connection = app.state.runtime.connection
    try:
        return ShinraiClient(connection.shinrai_url, connection.shinrai_key, http=app.state.http)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


def parse_models(body: Any) -> list[dict[str, Any]]:
    rows = body.get("data", body.get("models")) if isinstance(body, dict) else body
    if not isinstance(rows, list):
        raise TypeError("The models endpoint did not return a supported model list.")
    output = []
    for row in rows:
        if isinstance(row, str):
            output.append({"id": row})
        elif isinstance(row, dict) and isinstance(row.get("id", row.get("name")), str):
            output.append(
                {
                    "id": row.get("id", row.get("name")),
                    **{k: v for k, v in row.items() if k in {"owned_by", "unavailable", "degraded", "rag_support"}},
                }
            )
    if not output:
        raise ValueError("No selectable models were returned.")
    return output


def attachment_view(item: Attachment) -> dict[str, Any]:
    downloads = ["text"]
    downloads += ["pdf"] if item.protected_pdf else []
    downloads += ["image"] if item.protected_image else []
    downloads += ["mapping"] if item.mapping else []
    return {
        "id": item.id,
        "name": item.name,
        "media_type": item.media_type,
        "pages": len(item.page_images),
        "entities": item.entities,
        "protected_text": item.protected_text,
        "has_mapping": bool(item.mapping),
        "downloads": downloads,
        "note": item.note,
        "cleanup": item.cleanup,
    }


def attachment_views(state):
    return [attachment_view(item) for item in state.attachments.values()]


MEDIA_ALIASES = {"image/jpg": "image/jpeg", "image/x-ms-bmp": "image/bmp", "image/x-bmp": "image/bmp"}
EXTENSION_TYPES = {
    ".txt": "text/plain",
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".bmp": "image/bmp",
}


def upload_media_type(content_type: str | None, filename: str | None) -> str:
    """The browser's media type, or the extension's when the browser sent a generic one."""
    media_type = (content_type or "application/octet-stream").split(";", 1)[0].strip().lower()
    media_type = MEDIA_ALIASES.get(media_type, media_type)
    if media_type not in UPLOAD_TYPES and filename:
        return EXTENSION_TYPES.get(PurePath(filename).suffix.lower(), media_type)
    return media_type


def require_document_jobs(state: RuntimeState, media_type: str) -> None:
    """Fail before uploading when the connected deployment lists no v2 document jobs."""
    if media_type not in DOCUMENT_TYPES or not isinstance(state.capabilities, dict):
        return
    inputs = state.capabilities.get("inputs")
    if not isinstance(inputs, dict):
        return
    file_jobs = inputs.get("file", {}).get("jobs") if isinstance(inputs.get("file"), dict) else None
    if file_jobs not in {"ga", "beta"}:
        raise ValueError("This deployment does not serve API v2 document jobs (capabilities.inputs.file.jobs).")


def page_previews(result: dict[str, Any]) -> tuple[list[bytes], str]:
    """Preview images: the filled image, or the pages of the redacted PDF (none when rendering fails)."""
    if result.get("image"):
        return [result["image"]], ""
    try:
        return render_pdf(result.get("pdf", b"")), ""
    except ValueError as exc:
        # The redacted PDF stays available for download; only the previews are skipped.
        return [], str(exc)


def get_attachment(state, identifier):
    item = state.attachments.get(identifier)
    if not item:
        raise HTTPException(404, "Attachment was not found in this local session.")
    return item


def auth_for_path(path: str) -> str:
    if path.startswith(("/language/", "/text/analytics/", "/v1/azure/")):
        return "azure"
    if path.startswith(("/v2/projects/", "/v2/organizations/", "/v2/locations/", "/v2/infoTypes", "/v1/google/")):
        return "google"
    return "bearer"


def elapsed(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)


def make_trace(operation: str, **values):
    return {"id": str(uuid.uuid4()), "time": int(time.time()), "operation": operation, **values}


def complete_trace(trace: dict[str, Any], result: Result):
    trace.update(status="success", elapsed_ms=result.elapsed_ms, response=result.body, response_headers=result.headers)


def fail_trace(trace: dict[str, Any], exc: Exception):
    trace.update(status="error", error=exc.detail if isinstance(exc, HTTPException) else str(exc))
    if isinstance(exc, RemoteError):
        trace.update(http_status=exc.status, response=exc.body)
        if exc.cleanup:
            trace["cleanup"] = exc.cleanup


def trace_error(state, operation, exc, **values):
    trace = make_trace(operation, status="error", error=str(exc), **values)
    if isinstance(exc, RemoteError):
        trace.update(http_status=exc.status, response=exc.body)
    state.traces.insert(0, trace)


def api_error(exc):
    if isinstance(exc, RemoteError):
        return HTTPException(
            exc.status if 400 <= exc.status < 500 else 502,
            {"message": str(exc), "remote_status": exc.status, "remote_body": sanitize(exc.body)},
        )
    return HTTPException(422, str(exc))


app = create_app()
