from __future__ import annotations

import asyncio
import io
import json
import time
import uuid
from dataclasses import dataclass
from typing import Any

import httpx
import pypdfium2 as pdfium
from PIL import Image, UnidentifiedImageError

from .security import validate_base_url


class RemoteError(RuntimeError):
    def __init__(self, message: str, *, status: int = 0, body: Any = None):
        super().__init__(message)
        self.status = status
        self.body = body


@dataclass
class Result:
    status: int
    body: Any
    headers: dict[str, str]
    elapsed_ms: int


VISIBLE_HEADERS = {
    "content-type",
    "location",
    "operation-location",
    "x-request-id",
    "x-records-charged",
    "x-records-remaining",
    "x-shinrai-warnings",
    "x-shinrai-unmapped-entity-types",
    "x-amzn-requestid",
    "x-amzn-errortype",
    "retry-after",
}


class ShinraiClient:
    """Small no-retry client derived from the MIT-licensed shinrai-connect client."""

    def __init__(self, base_url: str, api_key: str, *, http: httpx.AsyncClient | None = None):
        self.base = validate_base_url(base_url)
        if not api_key:
            raise ValueError("A ShinrAI API key is required.")
        self.key = api_key
        self.http = http

    async def request(self, method: str, path: str, **kwargs: Any) -> Result:
        if (
            not path.startswith("/")
            or ".." in path
            or "\\" in path
            or path.startswith(("/console", "/ops", "/oauth", "/mcp"))
        ):
            raise ValueError("Unsupported ShinrAI path.")
        headers = {"Authorization": "Bearer " + self.key, **kwargs.pop("headers", {})}
        started = time.perf_counter()

        async def send(client: httpx.AsyncClient) -> httpx.Response:
            try:
                return await client.request(
                    method,
                    self.base + path,
                    headers=headers,
                    timeout=kwargs.pop("timeout", 300),
                    follow_redirects=False,
                    **kwargs,
                )
            except httpx.HTTPError as exc:
                raise RemoteError("ShinrAI could not be reached. The request was not retried.") from exc

        response = await send(self.http) if self.http else await self._temporary(send)
        elapsed = int((time.perf_counter() - started) * 1000)
        body = response_body(response)
        visible = {k.lower(): v for k, v in response.headers.items() if k.lower() in VISIBLE_HEADERS}
        if response.status_code >= 300:
            raise RemoteError(
                f"ShinrAI rejected the request (HTTP {response.status_code}).", status=response.status_code, body=body
            )
        return Result(response.status_code, body, visible, elapsed)

    @staticmethod
    async def _temporary(send):
        async with httpx.AsyncClient() as client:
            return await send(client)

    async def connect(self) -> tuple[Result, Result, set[tuple[str, str]]]:
        models, usage = await asyncio.gather(self.request("GET", "/v1/models"), self.request("GET", "/v1/usage"))
        routes: set[tuple[str, str]] = set()
        try:
            schema = await self.request("GET", "/openapi.json", timeout=30)
            routes = safe_schema_routes(schema.body)
        except (RemoteError, ValueError, TypeError):
            pass
        return models, usage, routes

    async def protect_batch(
        self,
        texts: list[str],
        *,
        mode: str = "replace",
        model: str = "shinrai-latest",
        tier: str = "standard",
        threshold: float = 0.7,
        known: dict[str, str] | None = None,
        include_entities: bool = True,
    ) -> Result:
        if mode not in {"replace", "mask", "label"} or tier not in {"standard", "batch", "realtime"}:
            raise ValueError("Unsupported protection option.")
        if not texts or len(texts) > 64 or any(not isinstance(text, str) or not text for text in texts):
            raise ValueError("Provide between one and 64 non-empty texts.")
        mapping = dict(known or {})
        return await self.request(
            "POST",
            "/v1/redact/batch",
            json={
                "texts": texts,
                "mode": mode,
                "model": model,
                "tier": tier,
                "threshold": threshold,
                "include_mapping": mode == "replace",
                "include_entities": include_entities,
                "known_replacements": mapping,
                "reserved_replacements": list(mapping.values()),
            },
        )

    async def document(
        self, content: bytes, content_type: str, *, mode: str = "replace", mapping: bool = True
    ) -> dict[str, Any]:
        if content_type in {"image/png", "image/jpeg", "image/bmp"}:
            content, content_type = image_to_pdf(content), "application/pdf"
        if content_type not in {
            "text/plain",
            "application/pdf",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        }:
            raise ValueError("Upload TXT, PDF, DOCX, PNG, JPEG, or BMP.")
        if not 0 < len(content) <= 10_000_000:
            raise ValueError("Files must be between 1 byte and 10 MB.")
        params = {"mode": mode, "include_mapping": str(mapping and mode == "replace").lower()}
        submitted = await self.request(
            "POST",
            "/v1/documents/jobs",
            params=params,
            content=content,
            headers={"Content-Type": content_type, "Idempotency-Key": str(uuid.uuid4())},
        )
        job_id = str(submitted.body.get("id", "")) if isinstance(submitted.body, dict) else ""
        try:
            uuid.UUID(job_id)
        except ValueError:
            raise RemoteError("ShinrAI returned an invalid document job identifier.") from None
        path = f"/v1/documents/jobs/{job_id}"
        deadline = time.monotonic() + 310
        cleanup = "pending"
        try:
            while time.monotonic() < deadline:
                job = await self.request("GET", path)
                status = job.body.get("status") if isinstance(job.body, dict) else None
                if status == "succeeded":
                    text, pdf = await asyncio.gather(
                        self.request("GET", path + "/artifacts/text"),
                        self.request("GET", path + "/artifacts/pdf"),
                    )
                    result = {
                        "job_id": job_id,
                        "job": job.body,
                        "text": text.body if isinstance(text.body, str) else "",
                        "pdf": pdf.body if isinstance(pdf.body, bytes) else b"",
                        "mapping": {},
                        "elapsed_ms": submitted.elapsed_ms + job.elapsed_ms + text.elapsed_ms + pdf.elapsed_ms,
                        "headers": {**submitted.headers, **job.headers},
                    }
                    if mapping and mode == "replace":
                        mapped = await self.request("GET", path + "/artifacts/mapping")
                        result["mapping"] = mapped.body if isinstance(mapped.body, dict) else {}
                        result["elapsed_ms"] += mapped.elapsed_ms
                    return result
                if status in {"failed", "cancelled"}:
                    raise RemoteError("The document could not be protected completely.", body=job.body)
                await asyncio.sleep(0.5)
            raise RemoteError("Document protection timed out.")
        finally:
            try:
                await self.request("DELETE", path, timeout=15)
                cleanup = "deleted"
            except RemoteError:
                cleanup = "remote cleanup not confirmed; server retention still applies"
            # A mutable result may have been prepared immediately before finally.
            if "result" in locals():
                result["cleanup"] = cleanup


def response_body(response: httpx.Response) -> Any:
    content_type = response.headers.get("content-type", "").lower()
    if "json" in content_type:
        try:
            return response.json()
        except ValueError:
            return response.text
    if content_type.startswith("text/") or content_type == "application/xml":
        return response.text
    return response.content


def safe_schema_routes(schema: Any) -> set[tuple[str, str]]:
    if not isinstance(schema, dict) or not isinstance(schema.get("paths"), dict):
        return set()
    prefixes = ("/v1/", "/language/", "/text/analytics/", "/providers/aws/", "/v2/")
    result = set()
    for path, operations in schema["paths"].items():
        if not isinstance(path, str) or not path.startswith(prefixes) or not isinstance(operations, dict):
            continue
        for method in operations:
            if method.upper() in {"GET", "POST", "PUT", "PATCH", "DELETE"}:
                result.add((method.upper(), path))
    return result


def image_to_pdf(content: bytes) -> bytes:
    try:
        with Image.open(io.BytesIO(content)) as source:
            source.seek(0)
            image = source.convert("RGB")
            if image.width * image.height > 8_000_000:
                raise ValueError("Image exceeds the 8-million-pixel limit.")
            output = io.BytesIO()
            image.save(output, format="PDF", resolution=144)
            return output.getvalue()
    except (UnidentifiedImageError, OSError) as exc:
        raise ValueError("The image could not be decoded.") from exc


def render_pdf(pdf: bytes, *, max_pages: int = 8) -> list[bytes]:
    if not pdf:
        return []
    try:
        document = pdfium.PdfDocument(pdf)
        if len(document) > max_pages:
            raise ValueError(f"Visual chat supports at most {max_pages} pages per attachment.")
        output: list[bytes] = []
        for page in document:
            bitmap = page.render(scale=1.5)
            image = bitmap.to_pil()
            buffer = io.BytesIO()
            image.save(buffer, format="PNG", optimize=True)
            output.append(buffer.getvalue())
            bitmap.close()
            page.close()
        document.close()
        return output
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError("The protected PDF preview could not be rendered.") from exc


def compact_json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
