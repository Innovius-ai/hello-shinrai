from __future__ import annotations

import base64
import hashlib
import io
import json
import re

import httpx
import pytest
from PIL import Image

from hello_shinrai.app import create_app

REMOTE_REQUESTS: list[httpx.Request] = []
V2_JOB_ID = "job_7f3a9c"
V2_UPLOAD_ID = "up_5b1e0d"
ORIGINALS = {"Ada Lovelace": "PERSON", "ada@example.org": "EMAIL"}
OCR_TEXT = "Contact Ada Lovelace at ada@example.org."
PROTECTED_TEXT = "Contact [PERSON_1] at [EMAIL_1]."
PROTECT_CALLS = [0]
JOB_ARTIFACTS: list[str] = []
# Document job deployments: the default host returns the redacted PDF as `protected` and the
# protected text as `text`; OLDER refuses `text` and answers `protected` with the protected text;
# PLAIN serves `text` but answers `protected` with text as well.
OLDER = "older.shinrai.example"
PLAIN = "plain.shinrai.example"


def pdf_bytes(pages: int = 1, color: str = "white") -> bytes:
    """A small image-only PDF, like the redacted PDF of a document job."""
    images = [Image.new("RGB", (120, 80), color) for _ in range(pages)]
    output = io.BytesIO()
    images[0].save(output, format="PDF", resolution=144, save_all=True, append_images=images[1:])
    return output.getvalue()


REDACTED_PDF = pdf_bytes(color="black")


def image_bytes(fmt: str = "PNG", color: str = "white") -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (160, 60), color).save(output, format=fmt)
    return output.getvalue()


def v2_inputs(body: dict) -> list[dict]:
    if "text" in body:
        return [{"id": "1", "kind": "text", "text": body["text"]}]
    if "texts" in body:
        return [{"id": str(index + 1), "kind": "text", "text": text} for index, text in enumerate(body["texts"])]
    return body["inputs"]


def v2_result(body: dict, *, protect: bool) -> dict:
    """A small stand-in for the PII API v2: surrogates are drawn per request unless mapping.known pins them."""
    PROTECT_CALLS[0] += protect
    number = PROTECT_CALLS[0]
    preset = (body.get("policy") or {}).get("preset", "pseudonymize")
    known = {entry["original"]: entry["replacement"] for entry in (body.get("mapping") or {}).get("known", [])}
    delta, results = [], []
    for item in v2_inputs(body):
        image = item["kind"] == "image"
        text = OCR_TEXT if image else item["text"]
        entities, output, cursor = [], [], 0
        for match in re.finditer("|".join(re.escape(value) for value in ORIGINALS), text):
            original, kind = match.group(0), ORIGINALS[match.group(0)]
            entity = {
                "id": f"e{len(entities) + 1}",
                "type": kind,
                "span": {"start": match.start(), "end": match.end()},
                "confidence": 0.98,
                "source": "model",
            }
            if image:
                entity["coords"] = {"boxes": [{"page": 1, "box": [10 * len(entities), 5, 40, 12]}]}
            if protect:
                if preset == "mask":
                    replacement = "*" * len(original)
                elif original in known:
                    replacement = known[original]
                else:
                    replacement = f"[{kind}_{number}]"
                    if not any(entry["original"] == original for entry in delta):
                        delta.append(
                            {
                                "id": f"mp_{len(delta) + 1}",
                                "type": kind,
                                "original": original,
                                "replacement": replacement,
                                "reversible": True,
                            }
                        )
                entity.update(action="surrogate", replacement=replacement)
                output.extend([text[cursor : match.start()], replacement])
                cursor = match.end()
            entities.append(entity)
        output.append(text[cursor:])
        row = {"input_id": item["id"], "status": "ok", "entities": entities}
        if protect and image:
            row["output"] = {
                "media_type": item["media_type"],
                "width": 160,
                "height": 60,
                "data_b64": base64.b64encode(image_bytes(item["media_type"].split("/")[1].upper(), "black")).decode(),
            }
        elif protect:
            row["output"] = "".join(output)
        if image:
            row["media"] = {"media_type": item["media_type"], "width": 160, "height": 60, "box_unit": "px"}
            if (body.get("output") or {}).get("include_text"):
                row["text"] = OCR_TEXT
        results.append(row)
    answer = {
        "request_id": "r1",
        "api_version": "2.0.0",
        "offset_unit": "codepoint",
        "engine": {"model": "v1.4", "layers_requested": ["model"], "layers_run": ["model"], "degraded": False},
        "results": results,
        "usage": {"records": 1, "charged": True, "tier": "standard"},
    }
    if protect and "mapping" in ((body.get("output") or {}).get("include") or []):
        answer["mapping"] = {"rev": len(known) + len(delta), "delta": [] if preset == "mask" else delta}
    return answer


def v2_response(request: httpx.Request) -> httpx.Response | None:
    path = request.url.path
    if request.url.host == "legacy.shinrai.example" and path.startswith("/v2/"):
        return httpx.Response(404, json={"detail": "Not Found"})
    if request.url.host == "disabled.shinrai.example" and path == "/v2/capabilities":
        return httpx.Response(501, json={"error": {"code": "capability_unavailable", "retryable": False}})
    if request.url.host == "local.shinrai.example" and path == "/v2/usage":
        return httpx.Response(501, json={"error": {"code": "capability_unavailable", "retryable": False}})
    if path == "/v2/capabilities":
        return httpx.Response(
            200,
            json={
                "api_version": "2.0.0",
                "profile": "public",
                "mode": "global",
                "models": [{"id": "v1.4", "status": "ga"}],
                "inputs": {
                    "text": {"standard": "ga", "realtime": "ga", "jobs": "beta"},
                    "file": {"standard": "unavailable", "realtime": "unavailable", "jobs": "beta"},
                },
                "tiers": {"standard": "allowed", "batch": "allowed", "realtime": "not_in_plan"},
                "actions": {},
                "restore": {"algorithms": ["shinrai-restore/1"]},
            },
        )
    if path == "/v2/usage":
        return httpx.Response(200, json={"plan": "starter", "available_records": 42.5, "last_30_days": {}})
    if path in {"/v2/detect", "/v2/protect"}:
        body = json.loads(request.content)
        if any("INCOMPLETE" in item.get("text", "") for item in v2_inputs(body)):
            return httpx.Response(200, json={"results": [], "engine": {"degraded": False}})
        return httpx.Response(
            200,
            json=v2_result(body, protect=path == "/v2/protect"),
            headers={"X-Records-Charged": "1", "X-Request-Id": "r1"},
        )
    if path == "/v2/restore":
        body = json.loads(request.content)
        reverse = {entry["replacement"]: entry["original"] for entry in body["mapping"]["known"]}
        results = []
        for item in body["inputs"]:
            text = item["text"]
            for replacement, original in reverse.items():
                text = text.replace(replacement, original)
            results.append({"input_id": item["id"], "text": text})
        return httpx.Response(200, json={"results": results, "restored": len(results)})
    if path == "/v2/uploads" and request.method == "POST":
        content = request.content
        return httpx.Response(
            201,
            json={
                "id": V2_UPLOAD_ID,
                "media_type": request.headers["content-type"],
                "bytes": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
                "expires_at": "2026-09-29T12:00:00Z",
            },
        )
    if path == "/v2/jobs" and request.method == "POST":
        body = json.loads(request.content)
        assert request.headers.get("idempotency-key")
        assert body["kind"] == "document" and body["inputs"][0]["source"] == {"upload": V2_UPLOAD_ID}
        artifacts = body["output"]["artifacts"]
        if request.url.host == OLDER and "text" in artifacts:
            return httpx.Response(
                422,
                json={
                    "error": {
                        "code": "validation_failed",
                        "message": "/output/artifacts: unknown or repeated artifact.",
                        "details": [{"pointer": "/output/artifacts", "reason": "unknown or repeated artifact"}],
                        "retryable": False,
                    }
                },
            )
        JOB_ARTIFACTS[:] = artifacts
        return httpx.Response(
            202,
            json={"id": V2_JOB_ID, "kind": "document", "status": "queued", "created_at": "2026-09-28T12:00:00Z"},
            headers={"Location": f"/v2/jobs/{V2_JOB_ID}"},
        )
    job = f"/v2/jobs/{V2_JOB_ID}"
    if path == job and request.method == "GET":
        return httpx.Response(
            200,
            json={
                "id": V2_JOB_ID,
                "kind": "document",
                "status": "succeeded",
                "created_at": "2026-09-28T12:00:00Z",
                "artifacts": {name: f"{job}/artifacts/{name}" for name in JOB_ARTIFACTS},
            },
        )
    if path == job and request.method == "DELETE":
        return httpx.Response(204)
    if path == f"{job}/artifacts/protected":
        if request.url.host in {OLDER, PLAIN}:
            return httpx.Response(200, text=PROTECTED_TEXT, headers={"content-type": "text/plain; charset=utf-8"})
        return httpx.Response(
            200,
            content=REDACTED_PDF,
            headers={"content-type": "application/pdf", "content-disposition": 'attachment; filename="protected.pdf"'},
        )
    if path == f"{job}/artifacts/text":
        return httpx.Response(200, text=PROTECTED_TEXT, headers={"content-type": "text/plain; charset=utf-8"})
    if path == f"{job}/artifacts/entities":
        entity = {"id": "e1", "type": "PERSON", "text": "Ada Lovelace", "span": {"start": 8, "end": 20}}
        return httpx.Response(200, json={"entities": [entity]})
    if path == f"{job}/artifacts/mapping":
        return httpx.Response(
            200,
            json={
                "delta": [
                    {
                        "id": "mp_1",
                        "type": "PERSON",
                        "original": "Ada Lovelace",
                        "replacement": "[PERSON_1]",
                        "reversible": True,
                    },
                    {
                        "id": "mp_2",
                        "type": "EMAIL",
                        "original": "ada@example.org",
                        "replacement": "[EMAIL_1]",
                        "reversible": True,
                    },
                ]
            },
        )
    return None


def llm_reply(flattened: str) -> str:
    person = re.search(r"\[PERSON_\d+\]", flattened)
    email = re.search(r"\[EMAIL_\d+\]", flattened)
    assert person, "the model must receive a surrogate"
    return f"I emailed {person.group(0)} at {email.group(0) if email else 'the address'}."


def remote_response(request: httpx.Request) -> httpx.Response:
    REMOTE_REQUESTS.append(request)
    if request.url.host == "models.example":
        return httpx.Response(200, json={"data": [{"id": "model-a"}, {"id": "model-b", "degraded": True}]})
    native = v2_response(request)
    if native is not None:
        return native
    if request.url.path == "/openapi.json":
        return httpx.Response(
            200,
            json={
                "paths": {
                    "/v1/analyze": {"post": {}},
                    "/v1/documents/jobs": {"post": {}},
                    "/v2/protect": {"post": {}},
                    "/v1/azure/language/:analyze-text": {"post": {}},
                    "/v1/aws/": {"post": {}},
                    "/console/account": {"get": {}},
                }
            },
        )
    if request.url.path in {"/providers/aws/credentials", "/v1/aws/providers/aws/credentials"}:
        return httpx.Response(
            200,
            json={
                "accessKeyId": "AKIASYNTHETIC",
                "secretAccessKey": "synthetic-signing-secret",
            },
        )
    if request.url.host == "llm.example" and request.url.path == "/v1/chat/completions":
        body = json.loads(request.content)
        flattened = json.dumps(body)
        assert "Ada Lovelace" not in flattened
        assert "ada@example.org" not in flattened
        assert "customer-secret" not in flattened
        reply = llm_reply(flattened)
        if body.get("stream"):
            split = reply.index("[PERSON_") + 4
            chunks = [
                {"choices": [{"delta": {"content": reply[:split]}}]},
                {"choices": [{"delta": {"content": reply[split:]}}]},
            ]
            content = "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks) + "data: [DONE]\n\n"
            return httpx.Response(200, text=content, headers={"content-type": "text/event-stream"})
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"role": "assistant", "content": reply}}],
                "usage": {"total_tokens": 12},
            },
        )
    if request.url.host == "llm.example" and request.url.path == "/v1/responses":
        body = json.loads(request.content)
        flattened = json.dumps(body)
        assert "Ada Lovelace" not in flattened
        person = re.search(r"\[PERSON_\d+\]", flattened).group(0)
        return httpx.Response(200, json={"output_text": f"Response for {person}.", "usage": {"total_tokens": 7}})
    if request.url.path.removeprefix("/v1/azure") in {
        "/language/:analyze-text",
        "/text/analytics/v3.1/entities/recognition/pii",
    }:
        assert request.headers.get("ocp-apim-subscription-key")
        assert request.headers.get("authorization") is None
        return httpx.Response(200, json={"provider": request.url.host, "results": {"documents": []}})
    if request.url.path.endswith("/content:inspect"):
        assert request.headers.get("x-goog-api-key") == "shinrai-secret"
        assert request.headers.get("authorization") is None
        return httpx.Response(200, json={"result": {"findings": []}})
    if request.url.path in {"/", "/v1/aws/"} and request.method == "POST":
        assert request.headers.get("x-amz-target") == "Comprehend_20171127.DetectPiiEntities"
        assert request.headers.get("authorization", "").startswith("AWS4-HMAC-SHA256")
        return httpx.Response(200, json={"Entities": []})
    return httpx.Response(404, json={"error": "not found"})


@pytest.fixture
async def local():
    REMOTE_REQUESTS.clear()
    PROTECT_CALLS[0] = 0
    JOB_ARTIFACTS[:] = ["protected", "text", "entities", "mapping"]
    upstream = httpx.AsyncClient(transport=httpx.MockTransport(remote_response))
    app = create_app(http=upstream)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            headers = {"X-Hello-Shinrai-Session": app.state.runtime.token, "Origin": "http://testserver"}
            yield app, client, headers
    await upstream.aclose()


@pytest.mark.asyncio
async def test_local_session_and_origin_are_required(local):
    _, client, headers = local
    assert (await client.get("/api/bootstrap")).status_code == 403
    assert (await client.get("/api/bootstrap", headers=headers)).status_code == 200
    hostile = {**headers, "Origin": "https://hostile.example"}
    assert (await client.get("/api/bootstrap", headers=hostile)).status_code == 403
    page = await client.get("/")
    assert page.status_code == 200
    assert "__SESSION_TOKEN__" not in page.text
    assert "Hello ShinrAI" in page.text


@pytest.mark.asyncio
async def test_connect_distinguishes_entitlement_and_filters_discovery(local):
    app, client, headers = local
    response = await client.post(
        "/api/settings/shinrai",
        headers=headers,
        json={
            "base_url": "https://api.shinrai.example",
            "api_key": "shinrai-secret",
            "remember": False,
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert "native_api" not in body
    assert body["capabilities"]["tiers"]["realtime"] == "not_in_plan"
    assert body["usage"]["available_records"] == 42.5
    assert {request.url.path for request in REMOTE_REQUESTS} >= {"/v2/capabilities", "/v2/usage"}
    assert not any(request.url.path.startswith("/v1/") for request in REMOTE_REQUESTS)
    # Native v1 routes of the deployment schema are not offered; vendor routes under /v1/<vendor> are.
    assert app.state.runtime.discovered_routes == {
        ("POST", "/v2/protect"),
        ("POST", "/v1/azure/language/:analyze-text"),
        ("POST", "/v1/aws/"),
    }
    catalog = (await client.get("/api/explorer/catalog", headers=headers)).json()["operations"]
    assert catalog[0]["group"] == "ShinrAI native API v2"
    assert not any(item["path"].startswith("/v1/") for item in catalog)
    by_id = {item["id"]: item for item in catalog}
    assert by_id["v2.protect"]["availability"] == "available"
    assert by_id["azure.sync"]["availability"] == "available"


@pytest.mark.asyncio
@pytest.mark.parametrize("host, status", [("legacy.shinrai.example", 404), ("disabled.shinrai.example", 501)])
async def test_connect_refuses_a_deployment_without_api_v2(local, host, status):
    app, client, headers = local
    response = await client.post(
        "/api/settings/shinrai",
        headers=headers,
        json={"base_url": f"https://{host}", "api_key": "shinrai-secret"},
    )
    assert response.status_code == (404 if status == 404 else 502)
    detail = response.json()["detail"]
    assert detail["remote_status"] == status
    assert detail["message"].startswith("This deployment does not serve the ShinrAI API v2")
    assert f"answered {status}" in detail["message"]
    assert "This version of Hello ShinrAI needs API v2" in detail["message"]
    assert not any(request.url.path.startswith("/v1/") for request in REMOTE_REQUESTS)
    bootstrap = (await client.get("/api/bootstrap", headers=headers)).json()
    assert bootstrap["capabilities"] is None and "native_api" not in bootstrap
    assert app.state.runtime.connection.shinrai_url != f"https://{host}"
    traces = (await client.get("/api/traces", headers=headers)).json()["traces"]
    assert traces[0]["operation"] == "shinrai.connect" and traces[0]["status"] == "error"


@pytest.mark.asyncio
async def test_connect_stays_on_v2_when_only_usage_is_not_metered(local):
    _, client, headers = local
    response = await client.post(
        "/api/settings/shinrai",
        headers=headers,
        json={"base_url": "https://local.shinrai.example", "api_key": "shinrai-secret"},
    )
    assert response.status_code == 200
    assert response.json()["capabilities"]["api_version"] == "2.0.0" and response.json()["usage"] is None
    assert not any(request.url.path.startswith("/v1/") for request in REMOTE_REQUESTS)


@pytest.mark.asyncio
async def test_model_discovery_preserves_common_catalogue_shape(local):
    _, client, headers = local
    response = await client.post(
        "/api/models/discover",
        headers=headers,
        json={
            "models_url": "https://models.example/v1/models",
            "api_key": "model-secret",
        },
    )
    assert response.status_code == 200
    assert response.json()["models"] == [{"id": "model-a"}, {"id": "model-b", "degraded": True}]
    assert (await client.get("/api/bootstrap", headers=headers)).json()["llm"]["model_count"] == 2


@pytest.mark.asyncio
async def test_model_discovery_rejects_remote_plain_http_cleanly(local):
    _, client, headers = local
    response = await client.post(
        "/api/models/discover",
        headers=headers,
        json={"models_url": "http://api.example.com/v1/models", "api_key": "secret"},
    )
    assert response.status_code == 422
    assert "Remote endpoints must use HTTPS" in response.json()["detail"]


@pytest.mark.asyncio
async def test_connection_settings_and_forget_keep_secrets_out_of_bootstrap(local):
    _, client, headers = local
    llm = await client.post(
        "/api/settings/llm",
        headers=headers,
        json={
            "provider": "custom",
            "models_url": "https://models.example/v1/models",
            "inference_url": "https://llm.example/v1/chat/completions",
            "protocol": "chat",
            "api_key": "llm-secret",
            "model": "model-a",
        },
    )
    azure = await client.post(
        "/api/settings/azure",
        headers=headers,
        json={"endpoint": "https://realazure.example", "api_key": "azure-secret"},
    )
    assert llm.status_code == azure.status_code == 200
    bootstrap = (await client.get("/api/bootstrap", headers=headers)).json()
    assert bootstrap["llm"]["has_key"] is True and bootstrap["azure"]["has_key"] is True
    assert "llm-secret" not in json.dumps(bootstrap) and "azure-secret" not in json.dumps(bootstrap)
    assert (await client.delete("/api/settings/secrets", headers=headers)).status_code == 200
    forgotten = (await client.get("/api/bootstrap", headers=headers)).json()
    assert forgotten["llm"]["has_key"] is False and forgotten["azure"]["has_key"] is False


@pytest.mark.asyncio
async def test_text_trace_is_private_locally_and_safe_when_exported(local):
    app, client, headers = local
    app.state.runtime.connection.shinrai_url = "https://api.shinrai.example"
    app.state.runtime.connection.shinrai_key = "shinrai-secret"
    response = await client.post(
        "/api/text",
        headers=headers,
        json={
            "operation": "redact",
            "text": "Ada Lovelace uses ada@example.org",
            "mode": "replace",
        },
    )
    assert response.status_code == 200
    assert response.json()["result"]["results"][0]["output"] == "[PERSON_1] uses [EMAIL_1]"
    traces = (await client.get("/api/traces", headers=headers)).json()["traces"]
    assert traces[0]["destination"] == "https://api.shinrai.example/v2/protect"
    assert traces[0]["response"]["mapping"]["delta"][0]["original"] == "Ada Lovelace"
    safe = (await client.get("/api/traces/export", headers=headers)).json()
    assert safe["traces"][0]["response"]["mapping"] == "[omitted from safe export]"
    full = (await client.get("/api/traces/export?include_sensitive=true", headers=headers)).json()
    assert full["traces"][0]["response"]["mapping"]["delta"][0]["replacement"] == "[PERSON_1]"


@pytest.mark.asyncio
async def test_chat_sends_only_protected_text_and_restores_reply(local):
    app, client, headers = local
    connection = app.state.runtime.connection
    connection.shinrai_url = "https://api.shinrai.example"
    connection.shinrai_key = "shinrai-secret"
    connection.llm_inference_url = "https://llm.example/v1/chat/completions"
    connection.llm_key = "llm-secret"
    connection.llm_model = "model-a"
    response = await client.post(
        "/api/chat",
        headers=headers,
        json={"text": "Email Ada Lovelace at ada@example.org", "stream": False, "tier": "standard"},
    )
    events = [json.loads(line) for line in response.text.splitlines()]
    assert response.status_code == 200
    assert events[-1]["type"] == "complete"
    assert app.state.runtime.chat.messages[-1]["content"] == "I emailed Ada Lovelace at ada@example.org."
    assert "llm-secret" not in response.text


@pytest.mark.asyncio
async def test_streaming_chat_restores_replacements_split_across_events(local):
    app, client, headers = local
    connection = app.state.runtime.connection
    connection.shinrai_url = "https://api.shinrai.example"
    connection.shinrai_key = "shinrai-secret"
    connection.llm_inference_url = "https://llm.example/v1/chat/completions"
    connection.llm_key = "llm-secret"
    connection.llm_model = "model-a"
    response = await client.post(
        "/api/chat",
        headers=headers,
        json={"text": "Email Ada Lovelace at ada@example.org", "stream": True, "tier": "standard"},
    )
    events = [json.loads(line) for line in response.text.splitlines()]
    assert response.status_code == 200
    assert "".join(event.get("restored", "") for event in events) == "I emailed Ada Lovelace at ada@example.org."
    assert events[-1]["type"] == "complete"
    assert events[-1]["trace"]["first_token_ms"] is not None


@pytest.mark.asyncio
async def test_responses_protocol_is_protected_and_restored(local):
    app, client, headers = local
    connection = app.state.runtime.connection
    connection.shinrai_url = "https://api.shinrai.example"
    connection.shinrai_key = "shinrai-secret"
    connection.llm_inference_url = "https://llm.example/v1/responses"
    connection.llm_protocol = "responses"
    connection.llm_key = "llm-secret"
    connection.llm_model = "model-a"
    response = await client.post(
        "/api/chat",
        headers=headers,
        json={"text": "Tell Ada Lovelace hello", "stream": False, "tier": "standard"},
    )
    events = [json.loads(line) for line in response.text.splitlines()]
    assert events[-1]["type"] == "complete"
    assert app.state.runtime.chat.messages[-1]["content"] == "Response for Ada Lovelace."


@pytest.mark.asyncio
async def test_protection_failure_stops_before_model_provider(local):
    app, client, headers = local
    connection = app.state.runtime.connection
    connection.shinrai_url = "https://api.shinrai.example"
    connection.shinrai_key = "shinrai-secret"
    connection.llm_inference_url = "https://llm.example/v1/chat/completions"
    connection.llm_key = "llm-secret"
    connection.llm_model = "model-a"
    response = await client.post(
        "/api/chat", headers=headers, json={"text": "INCOMPLETE", "stream": False, "tier": "standard"}
    )
    events = [json.loads(line) for line in response.text.splitlines()]
    assert events[-1]["type"] == "error"
    assert not any(request.url.host == "llm.example" for request in REMOTE_REQUESTS)


def connect_chat(app) -> None:
    connection = app.state.runtime.connection
    connection.shinrai_url = "https://api.shinrai.example"
    connection.shinrai_key = "shinrai-secret"
    connection.llm_inference_url = "https://llm.example/v1/chat/completions"
    connection.llm_key = "llm-secret"
    connection.llm_model = "model-a"


def v2_bodies(path: str) -> list[dict]:
    return [json.loads(request.content) for request in REMOTE_REQUESTS if request.url.path == path]


@pytest.mark.asyncio
async def test_text_file_uses_v2_protect_and_its_mapping(local):
    app, client, headers = local
    connect_chat(app)
    uploaded = await client.post(
        "/api/files",
        headers=headers,
        files={"file": ("customer-secret.txt", b"Ada Lovelace uses ada@example.org", "text/plain")},
        data={"mode": "replace"},
    )
    assert uploaded.status_code == 200, uploaded.text
    attachment = uploaded.json()["attachment"]
    assert attachment["pages"] == 0 and attachment["note"] == ""
    assert attachment["protected_text"] == "[PERSON_1] uses [EMAIL_1]"
    assert attachment["downloads"] == ["text", "mapping"]
    sent = v2_bodies("/v2/protect")[0]
    assert sent["policy"] == {"preset": "pseudonymize"} and sent["inputs"][0]["kind"] == "text"
    assert not any(request.url.path.startswith("/v1/") for request in REMOTE_REQUESTS)
    mapping = await client.get(f"/api/attachments/{attachment['id']}/download/mapping", headers=headers)
    assert {"original": "Ada Lovelace", "replacement": "[PERSON_1]"} in mapping.json()["mapping"]["known"]
    assert (await client.get(f"/api/attachments/{attachment['id']}/download/pdf", headers=headers)).status_code == 404
    response = await client.post(
        "/api/chat",
        headers=headers,
        json={
            "text": "Summarize Ada Lovelace",
            "attachment_ids": [attachment["id"]],
            "stream": False,
            "tier": "standard",
        },
    )
    assert json.loads(response.text.splitlines()[-1])["type"] == "complete"
    outgoing = next(request for request in REMOTE_REQUESTS if request.url.host == "llm.example").content.decode()
    assert "customer-secret" not in outgoing and "Ada Lovelace" not in outgoing and "ada@example.org" not in outgoing
    assert (await client.delete(f"/api/attachments/{attachment['id']}", headers=headers)).status_code == 200


@pytest.mark.asyncio
async def test_image_uses_v2_protect_with_filled_image_boxes_and_protected_visuals(local):
    app, client, headers = local
    connect_chat(app)
    uploaded = await client.post(
        "/api/files",
        headers=headers,
        # The browser label is wrong on purpose: the app sends the media type of the encoded image.
        files={"file": ("customer-secret.jpg", image_bytes("JPEG"), "image/png")},
        data={"mode": "replace", "language": "de"},
    )
    assert uploaded.status_code == 200, uploaded.text
    attachment = uploaded.json()["attachment"]
    sent = v2_bodies("/v2/protect")[0]
    assert sent["inputs"][0]["kind"] == "image" and sent["inputs"][0]["media_type"] == "image/jpeg"
    assert sent["output"]["include_text"] is True and "redaction_plan" in sent["output"]["include"]
    assert sent["detection"] == {"language": "de"}
    assert attachment["pages"] == 1 and attachment["entities"] == 2
    assert attachment["protected_text"] == "Contact [PERSON_1] at [EMAIL_1]."
    assert attachment["downloads"] == ["text", "image", "mapping"]
    image = await client.get(f"/api/attachments/{attachment['id']}/download/image", headers=headers)
    assert image.content.startswith(b"\x89PNG")
    assert Image.open(io.BytesIO(image.content)).getpixel((5, 5)) == (0, 0, 0)  # ShinrAI's filled output
    traces = (await client.get("/api/traces", headers=headers)).json()["traces"]
    shown = json.dumps(traces[0])
    assert "base64 characters" in shown and "image bytes" in shown
    safe = json.dumps((await client.get("/api/traces/export", headers=headers)).json())
    assert OCR_TEXT not in safe
    response = await client.post(
        "/api/chat",
        headers=headers,
        json={
            "text": "Summarize Ada Lovelace",
            "attachment_ids": [attachment["id"]],
            "include_visuals": True,
            "vision_confirmed": True,
            "stream": False,
            "tier": "standard",
        },
    )
    assert json.loads(response.text.splitlines()[-1])["type"] == "complete"
    outgoing = next(request for request in REMOTE_REQUESTS if request.url.host == "llm.example").content.decode()
    assert "data:image/png;base64," in outgoing
    assert "customer-secret" not in outgoing and "Ada Lovelace" not in outgoing and "ada@example.org" not in outgoing


@pytest.mark.asyncio
async def test_pdf_document_job_downloads_the_redacted_pdf_artifact_and_renders_previews(local):
    app, client, headers = local
    connect_chat(app)
    pdf = pdf_bytes()
    uploaded = await client.post(
        "/api/files",
        headers=headers,
        files={"file": ("customer-secret.pdf", pdf, "application/pdf")},
        data={"mode": "label"},
    )
    assert uploaded.status_code == 200, uploaded.text
    attachment = uploaded.json()["attachment"]
    upload = next(request for request in REMOTE_REQUESTS if request.url.path == "/v2/uploads")
    assert upload.content == pdf and upload.headers["content-type"] == "application/pdf"
    jobs = v2_bodies("/v2/jobs")
    assert len(jobs) == 1
    assert jobs[0]["kind"] == "document" and jobs[0]["policy"] == {"preset": "label"}
    assert jobs[0]["output"] == {"artifacts": ["protected", "text", "entities", "mapping"]}
    assert jobs[0]["inputs"][0]["media_type"] == "application/pdf"
    calls = {(request.method, request.url.path) for request in REMOTE_REQUESTS}
    assert {("GET", f"/v2/jobs/{V2_JOB_ID}/artifacts/{name}") for name in jobs[0]["output"]["artifacts"]} <= calls
    assert ("DELETE", f"/v2/jobs/{V2_JOB_ID}") in calls
    assert not any(request.url.path.startswith("/v1/") for request in REMOTE_REQUESTS)
    assert attachment["protected_text"] == PROTECTED_TEXT and attachment["note"] == ""
    assert attachment["cleanup"] == "deleted (job, upload and artifacts)"
    assert attachment["has_mapping"] is True and attachment["entities"] == 1 and attachment["pages"] == 1
    assert attachment["downloads"] == ["text", "pdf", "mapping"]
    assert app.state.runtime.attachments[attachment["id"]].mapping == {
        "Ada Lovelace": "[PERSON_1]",
        "ada@example.org": "[EMAIL_1]",
    }
    redacted = await client.get(f"/api/attachments/{attachment['id']}/download/pdf", headers=headers)
    assert redacted.status_code == 200 and redacted.content == REDACTED_PDF
    assert redacted.headers["content-type"] == "application/pdf"
    assert redacted.headers["content-disposition"] == "attachment; filename=protected.pdf"
    text = await client.get(f"/api/attachments/{attachment['id']}/download/text", headers=headers)
    assert text.text == PROTECTED_TEXT
    preview = await client.get(f"/api/attachments/{attachment['id']}/preview/1", headers=headers)
    assert preview.status_code == 200 and preview.content.startswith(b"\x89PNG")
    assert Image.open(io.BytesIO(preview.content)).getpixel((5, 5)) == (0, 0, 0)  # the redacted page
    trace = (await client.get("/api/traces", headers=headers)).json()["traces"][0]
    artifacts = trace["response"]["shinrai_response"]["artifacts"]
    assert artifacts["protected"]["content_type"] == "application/pdf"
    assert artifacts["protected"]["bytes"] == len(redacted.content)
    assert trace["response"]["pages_rendered"] == 1 and "refused" not in trace["shinrai_request"]
    safe = json.dumps((await client.get("/api/traces/export", headers=headers)).json())
    assert "Ada Lovelace" not in safe and "ada@example.org" not in safe
    assert "customer-secret" not in safe  # the uploaded file name
    full = json.dumps((await client.get("/api/traces/export?include_sensitive=true", headers=headers)).json())
    assert "customer-secret.pdf" in full
    response = await client.post(
        "/api/chat",
        headers=headers,
        json={
            "text": "Summarize the letter",
            "attachment_ids": [attachment["id"]],
            "include_visuals": True,
            "vision_confirmed": True,
            "stream": False,
            "tier": "standard",
        },
    )
    assert json.loads(response.text.splitlines()[-1])["type"] == "complete"
    outgoing = next(request for request in REMOTE_REQUESTS if request.url.host == "llm.example").content.decode()
    assert outgoing.count("data:image/png;base64,") == 1
    assert "customer-secret" not in outgoing and "Ada Lovelace" not in outgoing and "ada@example.org" not in outgoing
    assert (await client.delete(f"/api/attachments/{attachment['id']}", headers=headers)).status_code == 200
    assert (await client.get(f"/api/attachments/{attachment['id']}/preview/1", headers=headers)).status_code == 404


@pytest.mark.asyncio
async def test_pdf_document_job_falls_back_to_protected_text_on_an_older_deployment(local):
    app, client, headers = local
    connect_chat(app)
    app.state.runtime.connection.shinrai_url = f"https://{OLDER}"
    uploaded = await client.post(
        "/api/files",
        headers=headers,
        files={"file": ("customer-secret.pdf", pdf_bytes(), "application/pdf")},
        data={"mode": "replace"},
    )
    assert uploaded.status_code == 200, uploaded.text
    attachment = uploaded.json()["attachment"]
    first, second = v2_bodies("/v2/jobs")
    assert first["output"] == {"artifacts": ["protected", "text", "entities", "mapping"]}
    assert second["output"] == {"artifacts": ["protected", "entities", "mapping"]}
    assert {key: value for key, value in first.items() if key != "output"} == {
        key: value for key, value in second.items() if key != "output"
    }
    keys = [request.headers["idempotency-key"] for request in REMOTE_REQUESTS if request.url.path == "/v2/jobs"]
    assert len(set(keys)) == 2
    assert len([request for request in REMOTE_REQUESTS if request.url.path == "/v2/uploads"]) == 1
    assert ("DELETE", f"/v2/jobs/{V2_JOB_ID}") in {(request.method, request.url.path) for request in REMOTE_REQUESTS}
    assert not any(request.url.path.startswith("/v1/") for request in REMOTE_REQUESTS)
    assert attachment["protected_text"] == PROTECTED_TEXT and attachment["pages"] == 0
    assert attachment["downloads"] == ["text", "mapping"]
    assert "without a redacted PDF" in attachment["note"]
    missing = await client.get(f"/api/attachments/{attachment['id']}/download/pdf", headers=headers)
    assert missing.status_code == 404
    trace = (await client.get("/api/traces", headers=headers)).json()["traces"][0]
    assert trace["shinrai_request"]["refused"]["response"]["error"]["details"][0]["pointer"] == "/output/artifacts"
    assert trace["shinrai_request"]["job"]["output"]["artifacts"] == ["protected", "entities", "mapping"]


@pytest.mark.asyncio
async def test_document_job_shows_the_text_artifact_when_protected_is_not_a_pdf(local):
    app, client, headers = local
    connect_chat(app)
    app.state.runtime.connection.shinrai_url = f"https://{PLAIN}"
    uploaded = await client.post(
        "/api/files",
        headers=headers,
        files={"file": ("letter.docx", b"PK\x03\x04synthetic", "application/octet-stream")},
        data={"mode": "mask"},
    )
    assert uploaded.status_code == 200, uploaded.text
    attachment = uploaded.json()["attachment"]
    (job,) = v2_bodies("/v2/jobs")
    assert job["output"] == {"artifacts": ["protected", "text", "entities"]}
    assert job["inputs"][0]["media_type"] == "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    assert attachment["protected_text"] == PROTECTED_TEXT and attachment["pages"] == 0
    assert attachment["downloads"] == ["text"] and "without a redacted PDF" in attachment["note"]


@pytest.mark.asyncio
async def test_document_job_keeps_the_redacted_pdf_when_previews_exceed_the_page_limit(local):
    app, client, headers = local
    connect_chat(app)
    long_pdf = pdf_bytes(pages=9, color="black")

    def long_document(request: httpx.Request) -> httpx.Response:
        if request.url.path == f"/v2/jobs/{V2_JOB_ID}/artifacts/protected":
            REMOTE_REQUESTS.append(request)
            return httpx.Response(200, content=long_pdf, headers={"content-type": "application/pdf"})
        return remote_response(request)

    app.state.http = httpx.AsyncClient(transport=httpx.MockTransport(long_document))
    try:
        uploaded = await client.post(
            "/api/files",
            headers=headers,
            files={"file": ("long.pdf", pdf_bytes(pages=9), "application/pdf")},
            data={"mode": "replace"},
        )
    finally:
        await app.state.http.aclose()
    assert uploaded.status_code == 200, uploaded.text
    attachment = uploaded.json()["attachment"]
    assert attachment["pages"] == 0 and attachment["downloads"] == ["text", "pdf", "mapping"]
    assert attachment["note"] == "The redacted PDF has 9 pages. Page previews and visual chat support 8 at most."
    redacted = await client.get(f"/api/attachments/{attachment['id']}/download/pdf", headers=headers)
    assert redacted.content == long_pdf


@pytest.mark.asyncio
async def test_v2_document_jobs_are_refused_before_upload_when_not_served(local):
    app, client, headers = local
    connect_chat(app)
    app.state.runtime.capabilities = {"inputs": {"text": {"standard": "ga", "realtime": "ga", "jobs": "ga"}}}
    response = await client.post(
        "/api/files",
        headers=headers,
        files={"file": ("letter.docx", b"PK\x03\x04synthetic", "application/octet-stream")},
    )
    assert response.status_code == 422
    assert response.json()["detail"] == (
        "This deployment does not serve API v2 document jobs (capabilities.inputs.file.jobs)."
    )
    assert not REMOTE_REQUESTS


@pytest.mark.asyncio
async def test_azure_comparison_keeps_credentials_separate(local):
    app, client, headers = local
    app.state.runtime.connection.shinrai_url = "https://api.shinrai.example"
    app.state.runtime.connection.shinrai_key = "shinrai-secret"
    app.state.runtime.connection.azure_url = "https://realazure.example"
    app.state.runtime.connection.azure_key = "azure-secret"
    response = await client.post(
        "/api/azure/compare",
        headers=headers,
        json={
            "body": {
                "kind": "PiiEntityRecognition",
                "analysisInput": {"documents": [{"id": "1", "text": "Synthetic text"}]},
            },
            "include_real_azure": True,
        },
    )
    assert response.status_code == 200
    assert {item["label"] for item in response.json()["results"]} == {"ShinrAI", "Azure"}
    assert "shinrai-secret" not in response.text and "azure-secret" not in response.text


@pytest.mark.asyncio
async def test_aws_and_google_explorer_authentication_shapes(local):
    app, client, headers = local
    app.state.runtime.connection.shinrai_url = "https://api.shinrai.example"
    app.state.runtime.connection.shinrai_key = "shinrai-secret"
    aws = await client.post(
        "/api/explorer/run",
        headers=headers,
        json={"operation_id": "aws.detect"},
    )
    google = await client.post(
        "/api/explorer/run",
        headers=headers,
        json={"operation_id": "google.inspect"},
    )
    assert aws.status_code == google.status_code == 200
    assert aws.json()["result"] == {"Entities": []}
    assert google.json()["result"] == {"result": {"findings": []}}


@pytest.mark.asyncio
async def test_clear_chat_and_traces(local):
    app, client, headers = local
    app.state.runtime.chat.messages.append({"role": "user", "content": "synthetic"})
    app.state.runtime.traces.append({"operation": "synthetic", "status": "success"})
    assert (await client.delete("/api/chat", headers=headers)).status_code == 200
    assert (await client.delete("/api/traces", headers=headers)).status_code == 200
    assert app.state.runtime.chat.messages == []
    assert app.state.runtime.traces == []


@pytest.mark.asyncio
async def test_unknown_explorer_route_is_blocked(local):
    _, client, headers = local
    response = await client.post(
        "/api/explorer/run",
        headers=headers,
        json={
            "operation_id": "discovered",
            "method": "GET",
            "path": "/console/account",
        },
    )
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_catalogued_lifecycle_path_can_replace_ids_but_cannot_escape(local):
    app, client, headers = local
    app.state.runtime.connection.shinrai_url = "https://api.shinrai.example"
    app.state.runtime.connection.shinrai_key = "shinrai-secret"
    entities = await client.post(
        "/api/explorer/run",
        headers=headers,
        json={
            "operation_id": "v2.job-artifact",
            "path": f"/v2/jobs/{V2_JOB_ID}/artifacts/entities",
        },
    )
    assert entities.status_code == 200
    assert entities.json()["result"]["entities"][0]["type"] == "PERSON"
    for path in ("/console/account", f"/v1/documents/jobs/{V2_JOB_ID}/artifacts/pdf"):
        escaped = await client.post(
            "/api/explorer/run", headers=headers, json={"operation_id": "v2.job-artifact", "path": path}
        )
        assert escaped.status_code == 403


@pytest.mark.asyncio
async def test_explorer_downloads_binary_without_putting_it_in_trace_json(local):
    app, client, headers = local
    app.state.runtime.connection.shinrai_url = "https://api.shinrai.example"
    app.state.runtime.connection.shinrai_key = "shinrai-secret"
    response = await client.post(
        "/api/explorer/run",
        headers=headers,
        json={
            "operation_id": "v2.job-artifact",
            "path": f"/v2/jobs/{V2_JOB_ID}/artifacts/protected",
            "download": True,
        },
    )
    assert response.status_code == 200
    assert response.content.startswith(b"%PDF")
    traces = (await client.get("/api/traces", headers=headers)).json()["traces"]
    assert traces[0]["response"]["binary"] is True
    assert traces[0]["response"]["bytes"] == len(response.content)


@pytest.mark.asyncio
async def test_text_workspace_sends_native_v2_request_shapes(local):
    app, client, headers = local
    connect_chat(app)
    # A page loaded from an older version still sends "api"; the field is ignored and v2 answers.
    detect = await client.post(
        "/api/text", headers=headers, json={"operation": "analyze", "text": "Ada Lovelace", "api": "v1"}
    )
    assert detect.status_code == 200, detect.text
    assert detect.json()["result"]["results"][0]["entities"][0]["span"] == {"start": 0, "end": 12}
    batch = await client.post(
        "/api/text",
        headers=headers,
        json={
            "operation": "batch",
            "text": "Ada Lovelace\n---\nMail ada@example.org",
            "mode": "label",
            "tier": "realtime",
            "threshold": 0.4,
            "language": "en",
            "model": "shinrai-latest",
        },
    )
    assert batch.status_code == 200, batch.text
    detect_body, protect_body = v2_bodies("/v2/detect")[0], v2_bodies("/v2/protect")[0]
    assert detect_body == {
        "text": "Ada Lovelace",
        "detection": {"model": "latest"},
        "processing": {"tier": "standard"},
        "output": {"include": ["entities"]},
    }
    assert protect_body["texts"] == ["Ada Lovelace", "Mail ada@example.org"]
    assert protect_body["policy"] == {"preset": "label"}
    assert protect_body["output"] == {"include": ["entities", "mapping"]}
    assert protect_body["detection"] == {"model": "latest", "thresholds": {"default": 0.4}, "language": "en"}
    assert protect_body["processing"] == {"tier": "realtime"}
    assert [row["output"] for row in batch.json()["result"]["results"]] == ["[PERSON_1]", "Mail [EMAIL_1]"]
    assert not any(request.url.path.startswith("/v1/") for request in REMOTE_REQUESTS)
    bad = await client.post("/api/text", headers=headers, json={"text": "Ada", "language": "German"})
    assert bad.status_code == 422


@pytest.mark.asyncio
async def test_text_restore_runs_locally_or_through_v2_restore(local):
    app, client, headers = local
    connect_chat(app)
    missing = await client.post("/api/text", headers=headers, json={"operation": "restore", "text": "[PERSON_1]"})
    assert missing.status_code == 422
    protected = await client.post("/api/text", headers=headers, json={"text": "Ada Lovelace uses ada@example.org"})
    assert protected.status_code == 200
    calls = len(REMOTE_REQUESTS)
    local_restore = await client.post(
        "/api/text", headers=headers, json={"operation": "restore", "text": "Reply to [PERSON_1] at [EMAIL_1]."}
    )
    assert local_restore.status_code == 200
    assert local_restore.json()["result"]["results"][0]["text"] == "Reply to Ada Lovelace at ada@example.org."
    assert len(REMOTE_REQUESTS) == calls  # nothing left the computer
    remote = await client.post(
        "/api/text", headers=headers, json={"operation": "restore-remote", "text": "Reply to [PERSON_1]."}
    )
    assert remote.status_code == 200
    assert remote.json()["result"]["results"][0]["text"] == "Reply to Ada Lovelace."
    sent = v2_bodies("/v2/restore")[0]
    assert {"original": "Ada Lovelace", "replacement": "[PERSON_1]"} in sent["mapping"]["known"]
    assert sent["inputs"] == [{"id": "1", "text": "Reply to [PERSON_1]."}]
    safe = json.dumps((await client.get("/api/traces/export", headers=headers)).json())
    assert "Reply to Ada Lovelace" not in safe


@pytest.mark.asyncio
async def test_chat_sends_earlier_pairs_as_mapping_known_so_surrogates_stay(local):
    app, client, headers = local
    connect_chat(app)
    first = await client.post(
        "/api/chat", headers=headers, json={"text": "Email Ada Lovelace at ada@example.org", "stream": False}
    )
    second = await client.post(
        "/api/chat", headers=headers, json={"text": "Remind Ada Lovelace tomorrow", "stream": False}
    )
    assert (
        json.loads(first.text.splitlines()[-1])["type"]
        == json.loads(second.text.splitlines()[-1])["type"]
        == "complete"
    )
    turn_one, turn_two = v2_bodies("/v2/protect")
    assert "mapping" not in turn_one and turn_one["processing"] == {"tier": "realtime"}
    assert turn_one["policy"] == {"preset": "pseudonymize"}
    assert {"original": "Ada Lovelace", "replacement": "[PERSON_1]"} in turn_two["mapping"]["known"]
    assert "[PERSON_1]" in turn_two["mapping"]["reserved"]
    # History, reply and new prompt are protected together; every Ada is still [PERSON_1].
    assert [item["text"] for item in turn_two["inputs"]][-1] == "Remind Ada Lovelace tomorrow"
    outgoing = [request for request in REMOTE_REQUESTS if request.url.host == "llm.example"][-1].content.decode()
    assert "[PERSON_1]" in outgoing and "[PERSON_2]" not in outgoing
    assert app.state.runtime.chat.messages[-1]["content"] == "I emailed Ada Lovelace at ada@example.org."
    trace = (await client.get("/api/traces", headers=headers)).json()["traces"][0]
    assert trace["shinrai"]["endpoint"] == "/v2/protect"
    safe = (await client.get("/api/traces/export", headers=headers)).json()["traces"][0]
    assert safe["shinrai"]["request"]["mapping"] == "[omitted from safe export]"
    assert safe["shinrai"]["request"]["original_inputs"] == "[omitted from safe export]"


@pytest.mark.asyncio
async def test_explorer_runs_native_v2_operations(local):
    app, client, headers = local
    connect_chat(app)
    detect = await client.post("/api/explorer/run", headers=headers, json={"operation_id": "v2.detect"})
    restore = await client.post("/api/explorer/run", headers=headers, json={"operation_id": "v2.restore"})
    poll = await client.post(
        "/api/explorer/run", headers=headers, json={"operation_id": "v2.job-poll", "path": f"/v2/jobs/{V2_JOB_ID}"}
    )
    assert detect.status_code == restore.status_code == poll.status_code == 200
    assert detect.json()["result"]["results"][0]["entities"]
    assert restore.json()["result"]["results"][0]["text"] == "Ada Lovelace replied."
    assert poll.json()["result"]["status"] == "succeeded"
    assert [request.headers["authorization"] for request in REMOTE_REQUESTS] == ["Bearer shinrai-secret"] * 3
    escaped = await client.post(
        "/api/explorer/run", headers=headers, json={"operation_id": "v2.job-poll", "path": "/v2/jobs/a/b"}
    )
    assert escaped.status_code == 403
