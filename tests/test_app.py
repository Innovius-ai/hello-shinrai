from __future__ import annotations

import json

import httpx
import pytest

from hello_shinrai.app import create_app
from hello_shinrai.client import image_to_pdf

REMOTE_REQUESTS: list[httpx.Request] = []
DOCUMENT_ID = "42a75838-068b-4ed5-b975-6258eb25c397"


def protected_pdf() -> bytes:
    from io import BytesIO

    from PIL import Image

    output = BytesIO()
    Image.new("RGB", (120, 80), "white").save(output, format="PNG")
    return image_to_pdf(output.getvalue())


def remote_response(request: httpx.Request) -> httpx.Response:
    REMOTE_REQUESTS.append(request)
    if request.url.host == "models.example":
        return httpx.Response(200, json={"data": [{"id": "model-a"}, {"id": "model-b", "degraded": True}]})
    if request.url.path == "/v1/models":
        return httpx.Response(
            200,
            json={
                "models": [{"id": "shinrai-latest"}, {"id": "shinrai-medical", "status": "unavailable"}],
                "tiers": {
                    "standard": {"weight": 1, "allowed": True},
                    "batch": {"weight": 0.5, "allowed": True},
                    "realtime": {"weight": 1.6, "allowed": False},
                },
            },
        )
    if request.url.path == "/v1/usage":
        return httpx.Response(200, json={"balances": {"available_records": 42.5}, "plan": "starter"})
    if request.url.path == "/openapi.json":
        return httpx.Response(200, json={"paths": {"/v1/analyze": {"post": {}}, "/console/account": {"get": {}}}})
    if request.url.path == "/providers/aws/credentials":
        return httpx.Response(
            200,
            json={
                "accessKeyId": "AKIASYNTHETIC",
                "secretAccessKey": "synthetic-signing-secret",
            },
        )
    if request.url.path == "/v1/redact/batch":
        body = json.loads(request.content)
        if any("INCOMPLETE" in text for text in body["texts"]):
            return httpx.Response(200, json={"results": [], "mapping": {}})
        mapping = {"Ada Lovelace": "[PERSON_1]", "ada@example.org": "[EMAIL_1]"}
        results = []
        for text in body["texts"]:
            protected = text.replace("Ada Lovelace", "[PERSON_1]").replace("ada@example.org", "[EMAIL_1]")
            results.append({"text": protected, "entities": []})
        return httpx.Response(
            200,
            json={
                "results": results,
                "mapping": mapping,
                "tier": body.get("tier"),
                "usage": {"weighted_records": len(results)},
            },
        )
    if request.url.host == "llm.example" and request.url.path == "/v1/chat/completions":
        body = json.loads(request.content)
        flattened = json.dumps(body)
        assert "Ada Lovelace" not in flattened
        assert "ada@example.org" not in flattened
        assert "[PERSON_1]" in flattened
        assert "customer-secret.txt" not in flattened
        if body.get("stream"):
            chunks = [
                {"choices": [{"delta": {"content": "I emailed [PER"}}]},
                {"choices": [{"delta": {"content": "SON_1] at [EMAIL_1]."}}]},
            ]
            content = "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks) + "data: [DONE]\n\n"
            return httpx.Response(200, text=content, headers={"content-type": "text/event-stream"})
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"role": "assistant", "content": "I emailed [PERSON_1] at [EMAIL_1]."}}],
                "usage": {"total_tokens": 12},
            },
        )
    if request.url.host == "llm.example" and request.url.path == "/v1/responses":
        body = json.loads(request.content)
        flattened = json.dumps(body)
        assert "Ada Lovelace" not in flattened
        assert "[PERSON_1]" in flattened
        return httpx.Response(200, json={"output_text": "Response for [PERSON_1].", "usage": {"total_tokens": 7}})
    if request.url.path in {"/language/:analyze-text", "/text/analytics/v3.1/entities/recognition/pii"}:
        assert request.headers.get("ocp-apim-subscription-key")
        return httpx.Response(200, json={"provider": request.url.host, "results": {"documents": []}})
    if request.url.path.endswith("/content:inspect"):
        assert request.headers.get("x-goog-api-key") == "shinrai-secret"
        return httpx.Response(200, json={"result": {"findings": []}})
    if request.url.path == "/" and request.method == "POST":
        assert request.headers.get("x-amz-target") == "Comprehend_20171127.DetectPiiEntities"
        assert request.headers.get("authorization", "").startswith("AWS4-HMAC-SHA256")
        return httpx.Response(200, json={"Entities": []})
    if request.url.path == "/v1/documents/jobs" and request.method == "POST":
        return httpx.Response(202, json={"id": DOCUMENT_ID})
    if request.url.path == f"/v1/documents/jobs/{DOCUMENT_ID}" and request.method == "GET":
        return httpx.Response(200, json={"id": DOCUMENT_ID, "status": "succeeded"})
    if request.url.path.endswith("/artifacts/text"):
        return httpx.Response(200, text="Contact [PERSON_1] at [EMAIL_1].", headers={"content-type": "text/plain"})
    if request.url.path.endswith("/artifacts/pdf"):
        return httpx.Response(200, content=protected_pdf(), headers={"content-type": "application/pdf"})
    if request.url.path.endswith("/artifacts/mapping"):
        return httpx.Response(200, json={"Ada Lovelace": "[PERSON_1]", "ada@example.org": "[EMAIL_1]"})
    if request.url.path == f"/v1/documents/jobs/{DOCUMENT_ID}" and request.method == "DELETE":
        return httpx.Response(204)
    return httpx.Response(404, json={"error": "not found"})


@pytest.fixture
async def local():
    REMOTE_REQUESTS.clear()
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
    assert body["capabilities"]["tiers"]["realtime"]["allowed"] is False
    assert body["usage"]["balances"]["available_records"] == 42.5
    assert app.state.runtime.discovered_routes == {("POST", "/v1/analyze")}


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
    traces = (await client.get("/api/traces", headers=headers)).json()["traces"]
    assert traces[0]["response"]["mapping"]["Ada Lovelace"] == "[PERSON_1]"
    safe = (await client.get("/api/traces/export", headers=headers)).json()
    assert safe["traces"][0]["response"]["mapping"] == "[omitted from safe export]"
    full = (await client.get("/api/traces/export?include_sensitive=true", headers=headers)).json()
    assert full["traces"][0]["response"]["mapping"]["Ada Lovelace"] == "[PERSON_1]"


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


@pytest.mark.asyncio
async def test_attachment_uses_protected_artifacts_and_filename_never_reaches_llm(local):
    app, client, headers = local
    connection = app.state.runtime.connection
    connection.shinrai_url = "https://api.shinrai.example"
    connection.shinrai_key = "shinrai-secret"
    connection.llm_inference_url = "https://llm.example/v1/chat/completions"
    connection.llm_key = "llm-secret"
    connection.llm_model = "model-a"
    uploaded = await client.post(
        "/api/files",
        headers=headers,
        files={"file": ("customer-secret.txt", b"Ada Lovelace uses ada@example.org", "text/plain")},
        data={"mode": "replace"},
    )
    assert uploaded.status_code == 200
    attachment = uploaded.json()["attachment"]
    assert attachment["cleanup"] == "deleted"
    assert attachment["pages"] == 1
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
    assert response.status_code == 200
    llm_request = next(request for request in REMOTE_REQUESTS if request.url.host == "llm.example")
    outgoing = llm_request.content.decode()
    assert "customer-secret.txt" not in outgoing
    assert "Ada Lovelace" not in outgoing
    assert "ada@example.org" not in outgoing
    assert "data:image/png;base64," in outgoing
    text_download = await client.get(f"/api/attachments/{attachment['id']}/download/text", headers=headers)
    pdf_download = await client.get(f"/api/attachments/{attachment['id']}/download/pdf", headers=headers)
    assert text_download.text == "Contact [PERSON_1] at [EMAIL_1]."
    assert pdf_download.content.startswith(b"%PDF")
    assert (await client.delete(f"/api/attachments/{attachment['id']}", headers=headers)).status_code == 200
    assert (await client.get(f"/api/attachments/{attachment['id']}/preview/1", headers=headers)).status_code == 404


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
    poll = await client.post(
        "/api/explorer/run",
        headers=headers,
        json={
            "operation_id": "native.document-poll",
            "path": f"/v1/documents/jobs/{DOCUMENT_ID}",
        },
    )
    assert poll.status_code == 200
    assert poll.json()["result"]["status"] == "succeeded"
    escaped = await client.post(
        "/api/explorer/run",
        headers=headers,
        json={"operation_id": "native.document-poll", "path": "/console/account"},
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
            "operation_id": "native.document-download",
            "path": f"/v1/documents/jobs/{DOCUMENT_ID}/artifacts/pdf",
            "download": True,
        },
    )
    assert response.status_code == 200
    assert response.content.startswith(b"%PDF")
    traces = (await client.get("/api/traces", headers=headers)).json()["traces"]
    assert traces[0]["response"]["binary"] is True
    assert traces[0]["response"]["bytes"] == len(response.content)
