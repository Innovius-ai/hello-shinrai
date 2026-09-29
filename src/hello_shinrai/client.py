from __future__ import annotations

import asyncio
import base64
import copy
import hashlib
import io
import json
import re
import time
import uuid
from dataclasses import dataclass
from typing import Any

import httpx
import pypdfium2 as pdfium
from PIL import Image, UnidentifiedImageError

from .security import validate_base_url

VENDOR_GOOGLE = ("/v2/projects/", "/v2/organizations/", "/v2/locations/", "/v2/infoTypes")

# Native PII API v2 (https://shinrai.innovius.io/public-docs/pii-api-v2.md).
PRESETS = {"replace": "pseudonymize", "mask": "mask", "label": "label"}
LANGUAGE_PATTERN = r"^(auto|[a-z]{2,3}(-[A-Za-z0-9]{2,8})*)$"
V2_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")  # stricter than the contract: ids go into URL paths
V2_IMAGE_MAX_BYTES = 6 * 1024 * 1024
IMAGE_FORMATS = {"PNG": "image/png", "JPEG": "image/jpeg", "BMP": "image/bmp"}
TEXT_TYPE = "text/plain"
DOCUMENT_TYPES = {
    "application/pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}
UPLOAD_TYPES = {TEXT_TYPE, "image/png", "image/jpeg", "image/bmp", *DOCUMENT_TYPES}
# Document jobs: `protected` is the redacted PDF and `text` the protected text. Deployments that
# predate the redacted PDF refuse `text` and answer `protected` with the protected text.
DOCUMENT_ARTIFACTS = ("protected", "text", "entities")
TEXT_ONLY_DOCUMENT_ARTIFACTS = ("protected", "entities")
NO_API_V2 = (
    "This deployment does not serve the ShinrAI API v2 (GET /v2/capabilities answered {status}). "
    "Hello ShinrAI 0.2 needs API v2: use the hosted API or an offline release that serves API v2."
)


def v2_model(value: str | None) -> str:
    """v2 names models `latest` or `vX.Y`; the older alias `shinrai-latest` means `latest`."""
    value = (value or "").strip()
    return "latest" if value in {"", "latest", "shinrai-latest"} else value


def v2_options(
    *, model: str | None = None, tier: str = "standard", threshold: float | None = None, language: str | None = None
) -> dict[str, Any]:
    """Detection and processing blocks. Without a threshold the served confidence floor applies."""
    if tier not in {"standard", "batch", "realtime"}:
        raise ValueError("Unsupported processing tier.")
    detection: dict[str, Any] = {"model": v2_model(model)}
    if threshold is not None:
        detection["thresholds"] = {"default": threshold}
    if language and language != "auto":
        detection["language"] = language
    return {"detection": detection, "processing": {"tier": tier}}


def known_block(pairs: dict[str, str]) -> dict[str, Any]:
    """Earlier original/replacement pairs, so a later request keeps their surrogates."""
    return {
        "known": [{"original": original, "replacement": replacement} for original, replacement in pairs.items()],
        "reserved": sorted(set(pairs.values())),
    }


def reversible_pairs(entries: Any) -> dict[str, str]:
    """Restorable original -> replacement pairs from a v2 `mapping.delta`."""
    return merge_pairs({}, entries if isinstance(entries, list) else [])


def merge_pairs(existing: dict[str, str], entries: list[Any]) -> dict[str, str]:
    """Add pairs that keep the map restorable: non-empty, changed, and one original per replacement."""
    merged = dict(existing)
    used = set(merged.values())
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("reversible") is False:
            continue
        original, replacement = entry.get("original"), entry.get("replacement")
        if not isinstance(original, str) or not isinstance(replacement, str):
            continue
        if not original or not replacement or original == replacement or original in merged or replacement in used:
            continue
        merged[original] = replacement
        used.add(replacement)
    return merged


def mapping_delta(body: Any) -> list[Any]:
    mapping = body.get("mapping") if isinstance(body, dict) else None
    delta = mapping.get("delta") if isinstance(mapping, dict) else None
    return delta if isinstance(delta, list) else []


def apply_replacements(text: str, entities: list[Any]) -> str:
    """Rebuild protected text from code-point spans and their replacements (v2 offset_unit codepoint)."""
    rows = []
    for entity in entities:
        span = entity.get("span") if isinstance(entity, dict) else None
        replacement = entity.get("replacement") if isinstance(entity, dict) else None
        if not isinstance(span, dict) or not isinstance(replacement, str):
            continue
        start, end = span.get("start"), span.get("end")
        if isinstance(start, int) and isinstance(end, int) and 0 <= start <= end <= len(text):
            rows.append((start, end, replacement))
    output, cursor = [], 0
    for start, end, replacement in sorted(rows):
        if start < cursor:
            continue
        output.extend([text[cursor:start], replacement])
        cursor = end
    output.append(text[cursor:])
    return "".join(output)


def trace_view(body: Any) -> Any:
    """A v2 result for the trace: image bytes summarised, OCR text kept under an omitted key."""
    if not isinstance(body, dict):
        return body
    view = copy.deepcopy(body)
    for row in view.get("results") or []:
        if not isinstance(row, dict):
            continue
        output = row.get("output")
        if isinstance(output, dict) and isinstance(output.get("data_b64"), str):
            output["data_b64"] = f"[{len(output['data_b64'])} base64 characters]"
        if "text" in row and isinstance(row.get("media"), dict):
            row["ocr_text"] = row.pop("text")
        for entity in row.get("entities") or []:
            if isinstance(entity, dict) and "text" in entity:
                entity["original"] = entity.pop("text")
    return view


def entities_view(body: Any) -> Any:
    """An `entities` artifact for the trace: the original value of each entity under an omitted key."""
    if not isinstance(body, dict) or not isinstance(body.get("entities"), list):
        return body
    view = copy.deepcopy(body)
    for entity in view["entities"]:
        if isinstance(entity, dict) and "text" in entity:
            entity["original"] = entity.pop("text")
    return view


def refuses_text_artifact(exc: RemoteError) -> bool:
    """True when a deployment refused the requested artifacts before it created the job."""
    if exc.status not in {422, 501} or not isinstance(exc.body, dict):
        return False
    error = exc.body.get("error")
    details = error.get("details") if isinstance(error, dict) else None
    return any(
        isinstance(item, dict) and str(item.get("pointer", "")).startswith("/output/artifacts")
        for item in details or []
    )


def text_of(body: Any) -> str | None:
    if isinstance(body, bytes):
        return body.decode("utf-8", errors="replace")
    return body if isinstance(body, str) else None


def vendor_path(path: str) -> str:
    """Vendor compatibility calls go through /v1/<vendor> on the ShinrAI API host.

    The hosted API serves the Azure, Google and AWS contracts under these prefixes (the vendor
    hosts azure/google/aws.api.shinrai.innovius.io serve them at the vendor's own paths); every
    ShinrAI deployment, offline included, accepts the prefix.
    """
    if path.startswith(("/v1/azure/", "/v1/google/", "/v1/aws/")) or path == "/v1/aws":
        return path
    if path.startswith(("/language/", "/text/analytics/")):
        return "/v1/azure" + path
    if path.startswith(VENDOR_GOOGLE):
        return "/v1/google" + path
    if path.startswith("/providers/aws/"):
        return "/v1/aws" + path
    return path


class RemoteError(RuntimeError):
    def __init__(self, message: str, *, status: int = 0, body: Any = None):
        super().__init__(message)
        self.status = status
        self.body = body
        self.cleanup = ""


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
        path = vendor_path(path) if path.startswith("/") else path
        if (
            not path.startswith("/")
            or ".." in path
            or "\\" in path
            or path.startswith(("/console", "/ops", "/oauth", "/mcp"))
        ):
            raise ValueError("Unsupported ShinrAI path.")
        bearer_auth = kwargs.pop("bearer_auth", True)
        headers = kwargs.pop("headers", {})
        if bearer_auth:
            headers = {"Authorization": "Bearer " + self.key, **headers}
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
        """Capabilities and usage from API v2. A deployment without API v2 (404/501) is refused."""
        capabilities, usage = await asyncio.gather(
            self.request("GET", "/v2/capabilities"), self.request("GET", "/v2/usage"), return_exceptions=True
        )
        if isinstance(capabilities, RemoteError) and capabilities.status in {404, 501}:
            raise RemoteError(
                NO_API_V2.format(status=capabilities.status), status=capabilities.status, body=capabilities.body
            )
        if isinstance(capabilities, BaseException):
            raise capabilities
        if isinstance(usage, RemoteError) and usage.status in {404, 501}:
            # Local-mode deployments meter nothing: /v2/usage is optional there.
            usage = Result(usage.status, None, {}, 0)
        elif isinstance(usage, BaseException):
            raise usage
        routes: set[tuple[str, str]] = set()
        try:
            schema = await self.request("GET", "/openapi.json", timeout=30)
            routes = safe_schema_routes(schema.body)
        except (RemoteError, ValueError, TypeError):
            pass
        return capabilities, usage, routes

    async def protect_texts(
        self,
        texts: list[str],
        *,
        mode: str = "replace",
        model: str | None = None,
        tier: str = "standard",
        threshold: float | None = None,
        language: str | None = None,
        known: dict[str, str] | None = None,
    ) -> tuple[Result, dict[str, Any]]:
        """One POST /v2/protect for several texts; `known` pairs keep earlier surrogates."""
        if mode not in PRESETS:
            raise ValueError("Unsupported protection mode.")
        if not texts or len(texts) > 256 or any(not isinstance(text, str) or not text for text in texts):
            raise ValueError("Provide between one and 256 non-empty texts.")
        body: dict[str, Any] = {
            "inputs": [{"id": str(index + 1), "kind": "text", "text": text} for index, text in enumerate(texts)],
            **v2_options(model=model, tier=tier, threshold=threshold, language=language),
            "policy": {"preset": PRESETS[mode]},
            "output": {"include": ["entities", "mapping"]},
        }
        if known:
            body["mapping"] = known_block(known)
        return await self.request("POST", "/v2/protect", json=body), body

    async def protect_file_v2(
        self, content: bytes, content_type: str, *, mode: str = "replace", language: str | None = None
    ) -> dict[str, Any]:
        """Files through API v2: text and images with POST /v2/protect, PDF and DOCX as a document job."""
        if content_type not in UPLOAD_TYPES:
            raise ValueError("Upload TXT, PDF, DOCX, PNG, JPEG, or BMP.")
        if not 0 < len(content) <= 10_000_000:
            raise ValueError("Files must be between 1 byte and 10 MB.")
        if mode not in PRESETS:
            raise ValueError("Unsupported protection mode.")
        if content_type in DOCUMENT_TYPES:
            return await self.document_v2(content, content_type, mode=mode, language=language)
        output: dict[str, Any] = {"include": ["entities", "mapping"]}
        if content_type == TEXT_TYPE:
            try:
                item: dict[str, Any] = {"id": "1", "kind": "text", "text": content.decode("utf-8")}
            except UnicodeDecodeError:
                raise ValueError("Text files must be UTF-8.") from None
            shown = {**item, "text": f"[{len(content)} bytes of text]"}
        else:
            media_type = image_media_type(content)
            if len(content) > V2_IMAGE_MAX_BYTES:
                raise ValueError("API v2 accepts images up to 6 MiB. Send larger scans as a PDF document job.")
            item = {
                "id": "1",
                "kind": "image",
                "media_type": media_type,
                "data_b64": base64.b64encode(content).decode(),
            }
            shown = {**item, "data_b64": f"[{len(content)} image bytes]"}
            # Boxes per entity; the OCR text stays in this process and rebuilds the protected text.
            output = {"include": ["entities", "mapping", "redaction_plan"], "include_text": True}
        body: dict[str, Any] = {"inputs": [item], "policy": {"preset": PRESETS[mode]}, "output": output}
        if language and language != "auto":
            body["detection"] = {"language": language}
        answer = await self.request("POST", "/v2/protect", json=body)
        rows = answer.body.get("results") if isinstance(answer.body, dict) else None
        row = rows[0] if isinstance(rows, list) and rows and isinstance(rows[0], dict) else None
        if row is None or row.get("status", "ok") != "ok":
            raise RemoteError("ShinrAI returned no protection result for the file.", body=trace_view(answer.body))
        entities = row.get("entities") if isinstance(row.get("entities"), list) else []
        image = b""
        if item["kind"] == "text":
            if not isinstance(row.get("output"), str):
                raise RemoteError("ShinrAI returned no protected text.", body=trace_view(answer.body))
            protected_text = row["output"]
        else:
            filled = row.get("output") if isinstance(row.get("output"), dict) else {}
            try:
                image = png_bytes(base64.b64decode(filled.get("data_b64") or "", validate=True))
            except ValueError:
                raise RemoteError("ShinrAI returned no protected image.", body=trace_view(answer.body)) from None
            ocr_text = row.get("text") if isinstance(row.get("text"), str) else ""
            protected_text = apply_replacements(ocr_text, entities)
        return {
            "job_id": "",
            "job": None,
            "text": protected_text,
            "pdf": b"",
            "image": image,
            "mapping": reversible_pairs(mapping_delta(answer.body)) if mode != "mask" else {},
            "entities": len(entities),
            "request": {**body, "inputs": [shown]},
            "response": trace_view(answer.body),
            "elapsed_ms": answer.elapsed_ms,
            "headers": answer.headers,
            "cleanup": "not stored remotely (synchronous request)",
        }

    async def document_v2(
        self, content: bytes, content_type: str, *, mode: str = "replace", language: str | None = None
    ) -> dict[str, Any]:
        """PDF or DOCX: POST /v2/uploads, POST /v2/jobs (kind document), poll, download, DELETE the job.

        The job returns the redacted PDF (`protected`) and the protected text (`text`). A deployment
        without the redacted PDF refuses `text` before it creates a job; the app then submits once
        more without `text` and receives the protected text as `protected`.
        """
        uploaded = await self.request("POST", "/v2/uploads", content=content, headers={"Content-Type": content_type})
        upload_id = str(uploaded.body.get("id", "")) if isinstance(uploaded.body, dict) else ""
        if not V2_ID.fullmatch(upload_id):
            raise RemoteError("ShinrAI returned an invalid upload identifier.", body=uploaded.body)
        if uploaded.body.get("sha256") not in {None, hashlib.sha256(content).hexdigest()}:
            raise RemoteError("The upload checksum does not match the local file.", body=uploaded.body)
        mapping_artifact = ["mapping"] if mode != "mask" else []
        source: dict[str, Any] = {
            "id": "1",
            "kind": "file",
            "source": {"upload": upload_id},
            "media_type": content_type,
        }
        if language and language != "auto":
            source["language"] = language
        job_body: dict[str, Any] = {
            "kind": "document",
            "inputs": [source],
            "policy": {"preset": PRESETS[mode]},
            "output": {"artifacts": [*DOCUMENT_ARTIFACTS, *mapping_artifact]},
        }
        refused: dict[str, Any] | None = None
        try:
            try:
                submitted = await self._submit(job_body)
            except RemoteError as exc:
                if not refuses_text_artifact(exc):
                    raise
                # The deployment created no job and charged nothing: one more submission is safe.
                refused = {"job": job_body, "response": exc.body}
                job_body = {**job_body, "output": {"artifacts": [*TEXT_ONLY_DOCUMENT_ARTIFACTS, *mapping_artifact]}}
                submitted = await self._submit(job_body)
        except RemoteError as exc:
            exc.cleanup = "no job was created; the upload expires on the server after 24 hours"
            raise
        job_id = str(submitted.body.get("id", "")) if isinstance(submitted.body, dict) else ""
        if not V2_ID.fullmatch(job_id):
            raise RemoteError("ShinrAI returned an invalid job identifier.", body=submitted.body)
        path = f"/v2/jobs/{job_id}"
        try:
            job, files = await self._finished_job(path, job_body["output"]["artifacts"])
            protected_text, pdf = document_outputs(files)
            if protected_text is None and not pdf:
                raise RemoteError("ShinrAI returned no protected output for the document.", body=job.body)
        except BaseException as exc:
            cleanup = await self._delete_job(path)
            if isinstance(exc, RemoteError):
                exc.cleanup = cleanup
            raise
        entities_body = files["entities"].body if "entities" in files else None
        entities = entities_body.get("entities") if isinstance(entities_body, dict) else None
        mapping = files["mapping"].body if "mapping" in files else {}
        return {
            "job_id": job_id,
            "job": job.body,
            "text": protected_text or "",
            "pdf": pdf,
            "image": b"",
            "note": "" if pdf else "This deployment returned the protected text without a redacted PDF.",
            "mapping": reversible_pairs(mapping.get("delta") if isinstance(mapping, dict) else []),
            "entities": len(entities) if isinstance(entities, list) else 0,
            "request": {"upload": uploaded.body, "job": job_body, **({"refused": refused} if refused else {})},
            "response": {
                "job": job.body,
                "entities": entities_view(entities_body),
                "artifacts": {name: artifact_summary(item) for name, item in files.items()},
            },
            "elapsed_ms": uploaded.elapsed_ms
            + submitted.elapsed_ms
            + job.elapsed_ms
            + sum(item.elapsed_ms for item in files.values()),
            "headers": {**uploaded.headers, **submitted.headers, **job.headers},
            "cleanup": await self._delete_job(path),
        }

    async def _submit(self, job_body: dict[str, Any]) -> Result:
        return await self.request("POST", "/v2/jobs", json=job_body, headers={"Idempotency-Key": str(uuid.uuid4())})

    async def _finished_job(self, path: str, artifacts: list[str]) -> tuple[Result, dict[str, Result]]:
        """Poll until the job ends, then download the requested artifacts the job lists."""
        deadline = time.monotonic() + 310
        while time.monotonic() < deadline:
            job = await self.request("GET", path)
            status = job.body.get("status") if isinstance(job.body, dict) else None
            if status == "succeeded":
                listed = job.body.get("artifacts")
                names = [name for name in artifacts if not isinstance(listed, dict) or name in listed]
                downloads = await asyncio.gather(*(self.request("GET", f"{path}/artifacts/{name}") for name in names))
                return job, dict(zip(names, downloads, strict=True))
            if status in {"failed", "cancelled"}:
                raise RemoteError("The document could not be protected completely.", body=job.body)
            await asyncio.sleep(1.0)
        raise RemoteError("Document protection timed out.")

    async def _delete_job(self, path: str) -> str:
        try:
            await self.request("DELETE", path, timeout=15)
            return "deleted (job, upload and artifacts)"
        except (RemoteError, ValueError):
            return "remote cleanup not confirmed; server retention (24 hours) still applies"


def document_outputs(files: dict[str, Result]) -> tuple[str | None, bytes]:
    """The protected text and the redacted PDF of a document job.

    `protected` is the redacted PDF when it starts with `%PDF-`. Otherwise the deployment
    predates the redacted PDF and `protected` holds the protected text.
    """
    protected = files["protected"].body if "protected" in files else None
    pdf = protected if isinstance(protected, bytes) and protected.startswith(b"%PDF-") else b""
    text = text_of(files["text"].body) if "text" in files else None
    if text is None and not pdf:
        text = text_of(protected)
    return text, pdf


def artifact_summary(result: Result) -> dict[str, Any]:
    """Content type and size of a downloaded artifact; the trace never holds the content."""
    body = result.body
    size = len(body) if isinstance(body, bytes) else len(body.encode()) if isinstance(body, str) else None
    return {"content_type": result.headers.get("content-type", ""), "bytes": size}


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
    # API v2 and the vendor compatibility APIs; the native v1 routes are not offered.
    prefixes = ("/v1/azure/", "/v1/google/", "/v1/aws/", "/language/", "/text/analytics/", "/providers/aws/", "/v2/")
    result = set()
    for path, operations in schema["paths"].items():
        if not isinstance(path, str) or not path.startswith(prefixes) or not isinstance(operations, dict):
            continue
        for method in operations:
            if method.upper() in {"GET", "POST", "PUT", "PATCH", "DELETE"}:
                result.add((method.upper(), path))
    return result


def image_media_type(content: bytes) -> str:
    """The media type of the encoded image (browsers sometimes label files by extension only)."""
    try:
        with Image.open(io.BytesIO(content)) as source:
            fmt = source.format
    except (UnidentifiedImageError, OSError) as exc:
        raise ValueError("The image could not be decoded.") from exc
    if fmt not in IMAGE_FORMATS:
        raise ValueError("Upload a PNG, JPEG, or BMP image.")
    return IMAGE_FORMATS[fmt]


def png_bytes(content: bytes) -> bytes:
    """A PNG copy of an image ShinrAI returned, for previews and vision models."""
    try:
        with Image.open(io.BytesIO(content)) as source:
            source.seek(0)
            if source.format == "PNG":
                return content
            output = io.BytesIO()
            source.convert("RGBA" if source.mode in {"RGBA", "LA", "P"} else "RGB").save(output, format="PNG")
            return output.getvalue()
    except (UnidentifiedImageError, OSError) as exc:
        raise ValueError("The protected image could not be decoded.") from exc


def render_pdf(pdf: bytes, *, max_pages: int = 8) -> list[bytes]:
    """PNG page previews of a redacted PDF, for the Files view and visual chat."""
    if not pdf:
        return []
    try:
        document = pdfium.PdfDocument(pdf)
    except Exception as exc:
        raise ValueError("The redacted PDF could not be read.") from exc
    try:
        if len(document) > max_pages:
            raise ValueError(
                f"The redacted PDF has {len(document)} pages. Page previews and visual chat support {max_pages} at most."
            )
        output: list[bytes] = []
        for page in document:
            bitmap = page.render(scale=1.5)
            try:
                buffer = io.BytesIO()
                bitmap.to_pil().save(buffer, format="PNG", optimize=True)
                output.append(buffer.getvalue())
            finally:
                bitmap.close()
                page.close()
        return output
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError("The redacted PDF preview could not be rendered.") from exc
    finally:
        document.close()


def compact_json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
