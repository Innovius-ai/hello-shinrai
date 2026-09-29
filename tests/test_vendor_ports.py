from __future__ import annotations

import hashlib
import hmac
import json
import random
import re
import shutil
import socket
import subprocess
from urllib.parse import quote

import httpx
import pytest
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from botocore.credentials import Credentials

from hello_shinrai import app as app_module
from hello_shinrai import cli
from hello_shinrai.app import create_app
from hello_shinrai.exports import collection
from hello_shinrai.vendor_ports import vendor_openapi

KEY = "shinrai-secret-7f3a"
BASE_URL = "https://api.shinrai.example"
AWS_PAIR = {"accessKeyId": "AKIASYNTHETIC", "secretAccessKey": "synthetic-signing-secret"}
ORIGINAL = "Ada Lovelace"
UPSTREAM: list[httpx.Request] = []
PREFIX = re.compile(r"^http://127\.0\.0\.1:(\d+)(/p/[0-9a-f]{16})$")

OPENAPI = {
    "openapi": "3.1.0",
    "info": {"title": "ShinrAI API", "version": "2.0.0"},
    "tags": [
        {"name": "Native PII API v2"},
        {"name": "Azure compatibility"},
        {"name": "Google compatibility"},
        {"name": "AWS compatibility"},
    ],
    "paths": {
        "/v2/detect": {"post": {"tags": ["Native PII API v2"], "security": [{"BearerApiKey": []}]}},
        "/v1/analyze": {"post": {"tags": ["Native text"]}},
        "/v1/azure/language/:analyze-text": {
            "post": {"tags": ["Azure compatibility"], "security": [{"AzureSubscriptionKey": []}]}
        },
        "/v1/azure/language/analyze-text/jobs/{jobId}": {
            "get": {"tags": ["Azure compatibility"], "security": [{"AzureSubscriptionKey": []}]}
        },
        "/v1/google/v2/projects/{project}/locations/{location}/content:inspect": {
            "post": {"tags": ["Google compatibility"], "security": [{"GoogleApiKey": []}]}
        },
        "/v1/aws": {"post": {"tags": ["AWS compatibility"], "security": [{"AWSAccessKeyId": []}]}},
        "/v1/aws/providers/aws/credentials": {"post": {"tags": ["AWS compatibility"]}},
    },
    "components": {
        "securitySchemes": {
            "BearerApiKey": {"type": "http", "scheme": "bearer"},
            "AzureSubscriptionKey": {"type": "apiKey", "in": "header", "name": "Ocp-Apim-Subscription-Key"},
        }
    },
}


def upstream(request: httpx.Request) -> httpx.Response:
    """A ShinrAI deployment stand-in that echoes the original value, so tests can prove it is not traced."""
    UPSTREAM.append(request)
    path = request.url.path
    cors = {"access-control-allow-origin": "*", "x-request-id": "r-1"}
    if path == "/openapi.json":
        return httpx.Response(200, json=OPENAPI)
    if path == "/v1/aws/providers/aws/credentials":
        assert request.headers["authorization"] == "Bearer " + KEY
        return httpx.Response(200, json=AWS_PAIR)
    if path == "/v1/aws/":
        return httpx.Response(
            200,
            json={"Entities": [{"Type": "NAME", "Text": ORIGINAL}]},
            headers={"content-type": "application/x-amz-json-1.1", "x-amzn-RequestId": "aws-1", **cors},
        )
    if path == "/v1/azure/language/analyze-text/jobs" and request.method == "POST":
        location = f"{BASE_URL}/language/analyze-text/jobs/job-1?api-version=2026-05-01"
        return httpx.Response(202, headers={"Operation-Location": location, "x-ms-request-id": "job-1", **cors})
    if path == "/v1/azure/language/analyze-text/jobs/job-1":
        return httpx.Response(
            200,
            json={
                "jobId": "job-1",
                "status": "succeeded",
                "nextLink": f"{BASE_URL}/v1/azure/language/analyze-text/jobs/job-1?api-version=2026-05-01&top=20&skip=20",
            },
            headers=cors,
        )
    if path == "/v1/azure/language/:analyze-text":
        return httpx.Response(200, json={"results": {"documents": [{"entities": [{"text": ORIGINAL}]}]}}, headers=cors)
    if path.startswith("/v1/google/v2/projects/"):
        return httpx.Response(200, json={"result": {"findings": [{"quote": ORIGINAL}]}}, headers=cors)
    return httpx.Response(404, json={"error": {"code": "not_found"}})


class NoKeyring:
    @staticmethod
    def read(name):
        return ""

    @staticmethod
    def available():
        return False


def free_base() -> int:
    """A first port with three free loopback ports after it."""
    for _ in range(200):
        base = random.randint(20000, 60000)
        probes = []
        try:
            for port in range(base, base + 3):
                probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                probes.append(probe)
                probe.bind(("127.0.0.1", port))
            return base
        except OSError:
            continue
        finally:
            for probe in probes:
                probe.close()
    raise RuntimeError("no free loopback ports")


@pytest.fixture
async def workbench(monkeypatch):
    UPSTREAM.clear()
    monkeypatch.setattr(app_module, "keyring_module", lambda: NoKeyring)
    remote = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
    app = create_app(http=remote)
    async with app.router.lifespan_context(app):
        app.state.runtime.connection.shinrai_url = BASE_URL
        app.state.runtime.connection.shinrai_key = KEY
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as local:
            yield app, local, {"X-Hello-Shinrai-Session": app.state.runtime.token}
    await remote.aclose()


async def start(local, headers) -> dict:
    for _ in range(5):
        response = await local.post(
            "/api/vendor-ports", headers=headers, json={"enabled": True, "base_port": free_base()}
        )
        if response.status_code == 200:
            return response.json()
    raise AssertionError(response.text)


def sdk_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(trust_env=False, timeout=10)


def verify_sigv4(request: httpx.Request, secret: str) -> dict[str, str]:
    """Check a request the way the ShinrAI gateway does; return the credential scope."""
    algorithm, _, fields = request.headers["authorization"].partition(" ")
    credential, signed, signature = re.fullmatch(
        r"Credential=([^, ]+),\s*SignedHeaders=([^, ]+),\s*Signature=([a-f0-9]{64})", fields
    ).groups()
    scope = credential.split("/")
    names = signed.split(";")
    assert {"host", "x-amz-date", "x-amz-target"} <= set(names)
    headers = "".join(f"{name}:{' '.join(request.headers[name].split())}\n" for name in names)
    canonical = "\n".join(
        [
            "POST",
            quote(request.url.path, safe="/-_.~"),
            "",
            headers,
            signed,
            hashlib.sha256(request.content).hexdigest(),
        ]
    )
    to_sign = "\n".join(
        [algorithm, request.headers["x-amz-date"], "/".join(scope[1:]), hashlib.sha256(canonical.encode()).hexdigest()]
    )
    key = ("AWS4" + secret).encode()
    for part in scope[1:]:
        key = hmac.digest(key, part.encode(), "sha256")
    assert hmac.compare_digest(hmac.digest(key, to_sign.encode(), "sha256").hex(), signature)
    return {"access": scope[0], "region": scope[2], "service": scope[3]}


@pytest.mark.asyncio
async def test_vendor_endpoints_are_off_by_default_and_loopback_only(workbench):
    app, local, headers = workbench
    bootstrap = (await local.get("/api/bootstrap", headers=headers)).json()
    assert bootstrap["vendor_ports"] == {"enabled": False, "base_port": 8210, "endpoints": None}
    assert (await local.post("/api/vendor-ports", json={"enabled": True})).status_code == 403
    assert cli.parser().parse_args([]).vendor_ports is False
    assert cli.parser().parse_args(["--vendor-ports", "--vendor-ports-base", "9310"]).vendor_ports_base == 9310

    status = await start(local, headers)
    base = status["base_port"]
    prefixes = set()
    for offset, vendor in enumerate(("azure", "google", "aws")):
        port, prefix = PREFIX.fullmatch(status["endpoints"][vendor]).groups()
        assert int(port) == base + offset
        prefixes.add(prefix)
        assert app.state.vendor_ports.listeners[vendor][2].getsockname() == ("127.0.0.1", base + offset)
    assert len(prefixes) == 1

    endpoint = status["endpoints"]["azure"]
    body = {"kind": "PiiEntityRecognition", "analysisInput": {"documents": [{"id": "1", "text": "x"}]}}
    async with sdk_client() as sdk:
        url = endpoint + "/language/:analyze-text?api-version=2026-05-01"
        assert (await sdk.post(url, json=body)).status_code == 200
        # A foreign Host (DNS rebinding) and browser requests are refused; no CORS headers are returned.
        foreign = await sdk.post(url, json=body, headers={"Host": "attacker.example"})
        browser = await sdk.post(url, json=body, headers={"Origin": "https://attacker.example"})
        page = await sdk.get(endpoint + "/openapi.json", headers={"Sec-Fetch-Site": "cross-site"})
        # Node's fetch sends Sec-Fetch-Mode without Origin or Sec-Fetch-Site: it is not a browser.
        node = await sdk.post(url, json=body, headers={"Sec-Fetch-Mode": "cors"})
        preflight = await sdk.options(url, headers={"Access-Control-Request-Method": "POST"})
        assert foreign.status_code == browser.status_code == page.status_code == 403
        assert preflight.status_code == 404 and node.status_code == 200
        for response in (foreign, browser, page, preflight):
            assert not any(name.startswith("access-control-") for name in response.headers)
    assert len(UPSTREAM) == 2

    stopped = (await local.post("/api/vendor-ports", headers=headers, json={"enabled": False})).json()
    assert stopped["enabled"] is False and stopped["endpoints"] is None
    async with sdk_client() as sdk:
        with pytest.raises(httpx.TransportError):
            await sdk.post(endpoint + "/language/:analyze-text", json=body)


@pytest.mark.asyncio
async def test_cli_flag_starts_the_endpoints_with_the_app_and_stops_them_with_it(monkeypatch):
    monkeypatch.setattr(app_module, "keyring_module", lambda: NoKeyring)
    base = free_base()
    app = create_app(http=httpx.AsyncClient(transport=httpx.MockTransport(upstream)), vendor_ports=base)
    async with app.router.lifespan_context(app):
        status = app.state.vendor_ports.status()
        assert status["enabled"] and status["base_port"] == base
    assert app.state.vendor_ports.enabled is False
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", base))  # the port is free again


@pytest.mark.asyncio
async def test_azure_is_forwarded_with_the_saved_key_and_operation_location_is_rewritten(workbench):
    _, local, headers = workbench
    endpoint = (await start(local, headers))["endpoints"]["azure"]
    async with sdk_client() as sdk:
        submitted = await sdk.post(
            endpoint + "/language/analyze-text/jobs?api-version=2026-05-01&subscription-key=client-value",
            json={"analysisInput": {"documents": []}, "tasks": []},
            headers={"Ocp-Apim-Subscription-Key": "placeholder", "Authorization": "Bearer client-token"},
        )
        assert submitted.status_code == 202
        location = submitted.headers["operation-location"]
        assert location == endpoint + "/language/analyze-text/jobs/job-1?api-version=2026-05-01"
        assert submitted.headers["x-ms-request-id"] == "job-1"
        assert "access-control-allow-origin" not in submitted.headers
        polled = await sdk.get(location, headers={"Ocp-Apim-Subscription-Key": "placeholder"})
    sent = UPSTREAM[0]
    assert sent.url.path == "/v1/azure/language/analyze-text/jobs"
    assert sent.url.query == b"api-version=2026-05-01"
    assert sent.headers["ocp-apim-subscription-key"] == KEY
    assert "authorization" not in sent.headers
    assert UPSTREAM[1].url.path == "/v1/azure/language/analyze-text/jobs/job-1"
    assert UPSTREAM[1].headers["ocp-apim-subscription-key"] == KEY
    assert polled.status_code == 200
    assert polled.json()["nextLink"] == (
        endpoint + "/language/analyze-text/jobs/job-1?api-version=2026-05-01&top=20&skip=20"
    )


@pytest.mark.asyncio
async def test_google_is_forwarded_with_x_goog_api_key(workbench):
    _, local, headers = workbench
    endpoint = (await start(local, headers))["endpoints"]["google"]
    async with sdk_client() as sdk:
        response = await sdk.post(
            endpoint + "/v2/projects/hello/locations/global/content:inspect?key=placeholder&alt=json",
            json={"item": {"value": "x"}},
            headers={"x-goog-api-key": "placeholder", "Authorization": "Bearer client-token"},
        )
        wrong_vendor = await sdk.post(endpoint + "/language/:analyze-text", json={})
    assert response.status_code == 200
    assert wrong_vendor.status_code == 404
    assert len(UPSTREAM) == 1
    sent = UPSTREAM[0]
    assert sent.url.path == "/v1/google/v2/projects/hello/locations/global/content:inspect"
    assert sent.url.query == b"alt=json"
    assert sent.headers["x-goog-api-key"] == KEY
    assert "authorization" not in sent.headers


@pytest.mark.asyncio
async def test_aws_is_re_signed_with_the_issued_pair(workbench):
    _, local, headers = workbench
    endpoint = (await start(local, headers))["endpoints"]["aws"]
    payload = json.dumps({"Text": "x", "LanguageCode": "en"}).encode()
    async with sdk_client() as sdk:
        for _ in range(2):
            # As an AWS SDK sends it: signed with a placeholder pair for the local endpoint.
            request = AWSRequest(
                method="POST",
                url=endpoint,
                data=payload,
                headers={
                    "Content-Type": "application/x-amz-json-1.1",
                    "X-Amz-Target": "Comprehend_20171127.DetectPiiEntities",
                },
            )
            SigV4Auth(Credentials("placeholder", "placeholder"), "comprehend", "us-east-1").add_auth(request)
            response = await sdk.post(endpoint, content=payload, headers=dict(request.headers.items()))
            assert response.status_code == 200
            assert response.headers["x-amzn-requestid"] == "aws-1"
        credentials = await sdk.post(endpoint + "/providers/aws/credentials", json={})
    assert credentials.status_code == 404
    issued = [item for item in UPSTREAM if item.url.path == "/v1/aws/providers/aws/credentials"]
    calls = [item for item in UPSTREAM if item.url.path == "/v1/aws/"]
    assert len(issued) == 1 and len(calls) == 2
    for call in calls:
        assert call.content == payload
        assert call.headers["x-amz-target"] == "Comprehend_20171127.DetectPiiEntities"
        scope = verify_sigv4(call, AWS_PAIR["secretAccessKey"])
        assert scope == {"access": "AKIASYNTHETIC", "region": "eu-central-1", "service": "comprehend"}


@pytest.mark.asyncio
async def test_requests_without_the_run_prefix_are_refused_and_bodies_are_not_traced(workbench):
    _, local, headers = workbench
    status = await start(local, headers)
    endpoint = status["endpoints"]["azure"]
    port, prefix = PREFIX.fullmatch(endpoint).groups()
    root = f"http://127.0.0.1:{port}"
    body = {"kind": "PiiEntityRecognition", "analysisInput": {"documents": [{"id": "1", "text": ORIGINAL}]}}
    async with sdk_client() as sdk:
        refused = [
            await sdk.post(root + "/language/:analyze-text", json=body),
            await sdk.post(root + "/p/0123456789abcdef/language/:analyze-text", json=body),
            await sdk.post(root + prefix + "x/language/:analyze-text", json=body),
        ]
        native = await sdk.post(endpoint + "/v2/detect", json={"text": ORIGINAL})
        traversal = await sdk.post(endpoint + "/language/%2e%2e/%2e%2e/v2/detect", json={"text": ORIGINAL})
        before = len((await local.get("/api/traces", headers=headers)).json()["traces"])
        answered = await sdk.post(endpoint + "/language/:analyze-text?api-version=2026-05-01", json=body)
    assert [item.status_code for item in refused] == [404, 404, 404]
    assert all(prefix not in item.text for item in refused)
    assert native.status_code == 404 and traversal.status_code == 400
    assert UPSTREAM == [UPSTREAM[-1]] and UPSTREAM[-1].url.path == "/v1/azure/language/:analyze-text"
    assert answered.status_code == 200 and ORIGINAL in answered.text

    traces = (await local.get("/api/traces", headers=headers)).json()["traces"]
    vendor = [item for item in traces if item["operation"] == "vendor.azure"]
    # Only well-formed requests under the run prefix are traced: the native path and the forwarded call.
    assert len(vendor) == 2 and len(traces) == before + 1
    assert vendor[1]["request"] == {"method": "POST", "path": "/v2/detect"}  # refused before the body was read
    assert vendor[1]["status"] == "error" and vendor[1]["http_status"] == 404
    sent = len(answered.request.content)
    assert vendor[0]["request"] == {"method": "POST", "path": "/language/:analyze-text", "bytes": sent}
    assert vendor[0]["http_status"] == 200 and vendor[0]["response"]["bytes"] == len(answered.content)
    assert vendor[0]["response_headers"]["x-request-id"] == "r-1"
    assert vendor[0]["destination"] == f"{BASE_URL}/v1/azure/language/:analyze-text?api-version=2026-05-01"
    full = (await local.get("/api/traces/export?include_sensitive=true", headers=headers)).text
    for text in (json.dumps(traces), full):
        assert ORIGINAL not in text
        assert KEY not in text
        assert prefix not in text


@pytest.mark.asyncio
async def test_exports_contain_placeholders_never_the_key(workbench):
    _, local, headers = workbench
    assert (await local.get("/api/exports/collection/azure?target=local", headers=headers)).status_code == 409

    http = (await local.get("/api/exports/collection/azure?format=http", headers=headers)).text
    assert "@base = https://api.shinrai.example/v1/azure" in http
    assert "Ocp-Apim-Subscription-Key: {{SHINRAI_API_KEY}}" in http
    assert "POST {{base}}/language/:analyze-text?api-version=2026-05-01" in http
    google = (await local.get("/api/exports/collection/google?format=http", headers=headers)).text
    assert "x-goog-api-key: {{SHINRAI_API_KEY}}" in google
    curl = (await local.get("/api/exports/collection/aws?format=curl", headers=headers)).text
    assert '"$BASE/"' in curl and "BASE='https://api.shinrai.example/v1/aws'" in curl
    assert "--aws-sigv4 'aws:amz:eu-central-1:comprehend'" in curl
    assert 'Authorization: Bearer ${SHINRAI_API_KEY:?Set SHINRAI_API_KEY}"' in curl
    azure_curl = (await local.get("/api/exports/collection/azure?format=curl", headers=headers)).text
    assert '"$BASE/language/analyze-text/jobs/${1:?Pass job_id}?api-version=2026-05-01"' in azure_curl

    document = (await local.get("/api/exports/openapi/azure", headers=headers)).json()
    assert document["servers"][0]["url"] == "https://api.shinrai.example/v1/azure"
    assert set(document["paths"]) == {"/language/:analyze-text", "/language/analyze-text/jobs/{jobId}"}
    assert document["paths"]["/language/:analyze-text"]["post"]["security"] == [{"AzureSubscriptionKey": []}]

    status = await start(local, headers)
    local_http = (await local.get("/api/exports/collection/azure?format=http", headers=headers)).text
    assert f"@base = {status['endpoints']['azure']}" in local_http
    assert "SHINRAI_API_KEY" not in local_http and "Ocp-Apim-Subscription-Key" not in local_http
    local_aws = (await local.get("/api/exports/openapi/aws", headers=headers)).json()
    assert local_aws["servers"][0]["url"] == status["endpoints"]["aws"]
    assert set(local_aws["paths"]) == {"/"} and "security" not in local_aws["paths"]["/"]["post"]
    assert "securitySchemes" not in local_aws["components"]
    async with sdk_client() as sdk:
        served = (await sdk.get(status["endpoints"]["google"] + "/openapi.json")).json()
    assert list(served["paths"]) == ["/v2/projects/{project}/locations/{location}/content:inspect"]

    outputs = []
    for vendor in ("azure", "google", "aws"):
        for target in ("local", "deployment"):
            for fmt in ("http", "curl"):
                query = f"format={fmt}&target={target}"
                outputs.append((await local.get(f"/api/exports/collection/{vendor}?{query}", headers=headers)).text)
            outputs.append((await local.get(f"/api/exports/openapi/{vendor}?target={target}", headers=headers)).text)
    assert all(KEY not in text for text in outputs)


def test_curl_collections_are_valid_shell():
    shell = shutil.which("sh")
    for vendor in ("azure", "google", "aws"):
        for endpoint in (None, "http://127.0.0.1:8210/p/0123456789abcdef"):
            text = collection(vendor, fmt="curl", shinrai_url=BASE_URL, local_endpoint=endpoint, version="0.2.0")
            assert text.startswith("#!/bin/sh\n") and KEY not in text
            if shell:  # Windows runners may have no POSIX shell
                assert subprocess.run([shell, "-n"], input=text.encode(), check=False).returncode == 0


def test_vendor_openapi_accepts_prefixed_and_root_vendor_paths():
    root = {
        "paths": {
            "/language/:analyze-text": {"post": {}},
            "/v2/projects/{project}/locations/{location}/content:inspect": {"post": {}},
            "/v2/detect": {"post": {}},
            "/": {"post": {}},
            "/providers/aws/credentials": {"post": {}},
        },
        "info": {},
    }
    for document in (OPENAPI, root):
        google = vendor_openapi(document, "google", server_url="https://api.example/v1/google", local=False)
        assert list(google["paths"]) == ["/v2/projects/{project}/locations/{location}/content:inspect"]
        aws = vendor_openapi(document, "aws", server_url="https://api.example/v1/aws", local=False)
        assert set(aws["paths"]) == {"/", "/providers/aws/credentials"}
    with pytest.raises(LookupError):
        vendor_openapi({"paths": {"/v2/detect": {}}}, "azure", server_url="x", local=True)
