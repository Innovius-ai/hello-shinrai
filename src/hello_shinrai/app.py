from __future__ import annotations

import asyncio
import base64
import json
import time
import uuid
from contextlib import asynccontextmanager
from importlib.resources import files
from typing import Any, Literal
from urllib.parse import urlsplit

import httpx
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from botocore.credentials import Credentials
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import __version__
from .catalog import BY_ID, public_catalog, template_matches
from .client import RemoteError, Result, ShinraiClient, compact_json, render_pdf, response_body
from .restore import Restorer, restore
from .security import sanitize, validate_base_url
from .state import Attachment, RuntimeState


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


class TextRun(BaseModel):
    operation: Literal["analyze", "redact", "batch"] = "redact"
    text: str = Field(min_length=1, max_length=200_000)
    mode: Literal["replace", "mask", "label"] = "replace"
    model: str = "shinrai-latest"
    tier: Literal["standard", "batch", "realtime"] = "standard"
    threshold: float = Field(default=0.7, ge=0, le=1)


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
    shinrai_model: str = "shinrai-latest"
    tier: Literal["standard", "batch", "realtime"] = "realtime"
    threshold: float = Field(default=0.7, ge=0, le=1)


class AzureSettings(BaseModel):
    endpoint: str = ""
    api_key: str = ""
    remember: bool = False


class AzureCompare(BaseModel):
    path: str = "/language/:analyze-text"
    api_version: str = "2026-05-01"
    body: dict[str, Any]
    include_real_azure: bool = False


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
    yield


def create_app(*, http: httpx.AsyncClient | None = None) -> FastAPI:
    app = FastAPI(title="Hello ShinrAI", version=__version__, docs_url=None, redoc_url=None, lifespan=lifespan)
    app.state.runtime = RuntimeState()
    app.state.http = http
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
            "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; connect-src 'self'"
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
            models, usage, routes = await client.connect()
        except (ValueError, RemoteError) as exc:
            trace_error(state, "shinrai.connect", exc, destination=body.base_url)
            raise api_error(exc)
        state.connection.shinrai_url, state.connection.shinrai_key = url, key
        state.capabilities, state.usage, state.discovered_routes = models.body, usage.body, routes
        saved = keyring_module().write("shinrai", key) if body.remember else False
        state.traces.insert(
            0,
            make_trace(
                "shinrai.connect",
                destination=url,
                status="success",
                elapsed_ms=models.elapsed_ms + usage.elapsed_ms,
                response={"models": models.body, "usage": usage.body},
                response_headers={**models.headers, **usage.headers},
            ),
        )
        return {
            "capabilities": models.body,
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
        state.aws_credentials = None
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
        client = configured_shinrai(app)
        path = "/v1/analyze" if body.operation == "analyze" else "/v1/redact/batch"
        request_body: dict[str, Any]
        if body.operation == "analyze":
            request_body = {"text": body.text, "model": body.model, "tier": body.tier, "threshold": body.threshold}
        else:
            texts = [part.strip() for part in body.text.split("\n---\n")] if body.operation == "batch" else [body.text]
            request_body = {
                "texts": texts,
                "mode": body.mode,
                "model": body.model,
                "tier": body.tier,
                "threshold": body.threshold,
                "include_mapping": body.mode == "replace",
                "include_entities": True,
            }
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
        return {"result": result.body, "trace": sanitize(trace, include_sensitive=True)}

    @app.post("/api/files")
    async def upload_file(
        file: UploadFile = File(...),  # noqa: B008 - FastAPI dependency marker
        mode: Literal["replace", "mask", "label"] = Form("replace"),
    ):
        state: RuntimeState = app.state.runtime
        content = await file.read(10_000_001)
        media_type = (file.content_type or "application/octet-stream").split(";", 1)[0].lower()
        trace = make_trace(
            "file.protect",
            destination=state.connection.shinrai_url + "/v1/documents/jobs",
            request={
                "content_type": media_type,
                "bytes": len(content),
                "mode": mode,
                "original_filename": file.filename,
            },
            status="running",
        )
        state.traces.insert(0, trace)
        try:
            result = await configured_shinrai(app).document(content, media_type, mode=mode, mapping=mode == "replace")
            pages = render_pdf(result["pdf"])
        except (ValueError, RemoteError) as exc:
            fail_trace(trace, exc)
            raise api_error(exc)
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
        )
        state.attachments[identifier] = attachment
        trace.update(
            status="success",
            elapsed_ms=result["elapsed_ms"],
            response={
                "job": result["job"],
                "protected_text": result["text"],
                "mapping": result["mapping"],
                "pages_rendered": len(pages),
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
    async def attachment_download(identifier: str, kind: Literal["text", "pdf", "mapping"]):
        attachment = get_attachment(app.state.runtime, identifier)
        if kind == "text":
            return Response(
                attachment.protected_text,
                media_type="text/plain",
                headers={"Content-Disposition": "attachment; filename=protected.txt"},
            )
        if kind == "pdf":
            return Response(
                attachment.protected_pdf,
                media_type="application/pdf",
                headers={"Content-Disposition": "attachment; filename=protected.pdf"},
            )
        return JSONResponse(attachment.mapping, headers={"Content-Disposition": "attachment; filename=protected.json"})

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
                app, state.connection.shinrai_url, state.connection.shinrai_key, body.path, query, body.body, "ShinrAI"
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
            destination=state.connection.shinrai_url + path,
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
        protected = await configured_shinrai(app).protect_batch(
            originals,
            mode=body.mode,
            model=body.shinrai_model,
            tier=body.tier,
            threshold=body.threshold,
            known=state.chat.mapping,
            include_entities=True,
        )
        values = protected.body.get("results", [])
        if len(values) != len(originals) or any(not isinstance(row.get("text"), str) for row in values):
            raise RemoteError("ShinrAI returned an incomplete protection result.", body=protected.body)
        if body.mode == "replace":
            returned_mapping = protected.body.get("mapping", {})
            if not isinstance(returned_mapping, dict):
                raise RemoteError("ShinrAI returned an invalid replacement map.")
            state.chat.mapping.update(returned_mapping)
        else:
            state.chat.mapping.clear()
        protected_by_name = {name: values[index]["text"] for name, index in positions}
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
                "request": {"texts": originals, "mode": body.mode, "known_replacements": state.chat.mapping},
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
                    restorer = Restorer(state.chat.mapping if body.mode == "replace" else {})
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
                restored_text = restore(provider_text, state.chat.mapping) if body.mode == "replace" else provider_text
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
    state: RuntimeState = app.state.runtime
    client = configured_shinrai(app)
    if not state.aws_credentials:
        issued = await client.request("POST", "/providers/aws/credentials")
        state.aws_credentials = issued.body
    pair = state.aws_credentials or {}
    access, secret = pair.get("accessKeyId"), pair.get("secretAccessKey")
    if not access or not secret:
        raise RemoteError("ShinrAI did not issue valid AWS SDK credentials.")
    body = compact_json(payload)
    url = state.connection.shinrai_url + "/"
    request = AWSRequest(
        method="POST",
        url=url,
        data=body,
        headers={"Content-Type": "application/x-amz-json-1.1", "X-Amz-Target": operation["aws_target"]},
    )
    SigV4Auth(Credentials(access, secret), "comprehend", "eu-central-1").add_auth(request)
    started = time.perf_counter()
    async with http_client(app) as http:
        response = await http.post(
            url, content=body, headers=dict(request.headers.items()), timeout=300, follow_redirects=False
        )
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
    return {
        "id": item.id,
        "name": item.name,
        "media_type": item.media_type,
        "pages": len(item.page_images),
        "protected_text": item.protected_text,
        "has_mapping": bool(item.mapping),
        "cleanup": item.cleanup,
    }


def attachment_views(state):
    return [attachment_view(item) for item in state.attachments.values()]


def get_attachment(state, identifier):
    item = state.attachments.get(identifier)
    if not item:
        raise HTTPException(404, "Attachment was not found in this local session.")
    return item


def auth_for_path(path: str) -> str:
    if path.startswith(("/language/", "/text/analytics/")):
        return "azure"
    if path.startswith("/v2/"):
        return "google"
    return "bearer"


def elapsed(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)


def make_trace(operation: str, **values):
    return {"id": str(uuid.uuid4()), "time": int(time.time()), "operation": operation, **values}


def complete_trace(trace: dict[str, Any], result: Result):
    trace.update(status="success", elapsed_ms=result.elapsed_ms, response=result.body, response_headers=result.headers)


def fail_trace(trace: dict[str, Any], exc: Exception):
    trace.update(status="error", error=str(exc))
    if isinstance(exc, RemoteError):
        trace.update(http_status=exc.status, response=exc.body)


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
