from __future__ import annotations

import io

import pytest
from PIL import Image

from hello_shinrai import cli, credentials
from hello_shinrai.app import page_previews
from hello_shinrai.catalog import BY_ID, OPERATIONS, public_catalog, template_matches
from hello_shinrai.client import render_pdf, safe_schema_routes
from hello_shinrai.restore import Restorer, restore
from hello_shinrai.security import sanitize, validate_base_url


def test_restoration_handles_split_stream_tokens():
    value = Restorer({"Ada Lovelace": "[PERSON_1]"})
    assert value.feed("Hello [PER") == "Hello "
    assert value.feed("SON_1], welcome") == "Ada Lovelace, welcome"
    assert value.finish() == ""
    assert restore("Ask [PERSON_1]", {"Ada Lovelace": "[PERSON_1]"}) == "Ask Ada Lovelace"


def test_url_policy_allows_loopback_http_and_remote_https_only():
    assert validate_base_url("http://127.0.0.1:8000") == "http://127.0.0.1:8000"
    assert validate_base_url("https://api.example.com") == "https://api.example.com"
    assert validate_base_url("https://api.example.com/v1/models", allow_path=True).endswith("/v1/models")
    with pytest.raises(ValueError):
        validate_base_url("http://api.example.com")
    with pytest.raises(ValueError):
        validate_base_url("https://key:secret@example.com")


def test_safe_export_masks_credentials_signed_urls_and_private_values():
    value = {
        "Authorization": "Bearer secret-token",
        "mapping": {"Ada": "Person 1"},
        "original_input": "Ada",
        "url": "https://storage.example/file?sv=1&sig=secret&visible=yes",
    }
    safe = sanitize(value)
    assert safe["Authorization"] == "[hidden]"
    assert safe["mapping"] == "[omitted from safe export]"
    assert "secret" not in safe["url"]
    full = sanitize(value, include_sensitive=True)
    assert full["mapping"] == {"Ada": "Person 1"}
    assert full["Authorization"] == "[hidden]"


def test_openapi_discovery_keeps_only_processing_routes():
    schema = {
        "paths": {
            "/v1/analyze": {"post": {}},
            "/v1/redact/batch": {"post": {}},
            "/v1/documents/jobs/{identifier}": {"get": {}},
            "/v1/usage": {"get": {}},
            "/v1/azure/language/:analyze-text": {"post": {}},
            "/v1/google/v2/projects/{project}/content:inspect": {"post": {}},
            "/v1/aws/": {"post": {}},
            "/providers/aws/credentials": {"post": {}},
            "/v2/protect": {"post": {}},
            "/v2/projects/{project}/content:inspect": {"post": {}},
            "/console/account": {"get": {}},
            "/ops/health": {"get": {}},
        }
    }
    assert safe_schema_routes(schema) == {
        ("POST", "/v1/azure/language/:analyze-text"),
        ("POST", "/v1/google/v2/projects/{project}/content:inspect"),
        ("POST", "/v1/aws/"),
        ("POST", "/providers/aws/credentials"),
        ("POST", "/v2/protect"),
        ("POST", "/v2/projects/{project}/content:inspect"),
    }


def pdf_pages(count: int) -> bytes:
    images = [Image.new("RGB", (200, 100), "black") for _ in range(count)]
    output = io.BytesIO()
    images[0].save(output, format="PDF", resolution=144, save_all=True, append_images=images[1:])
    return output.getvalue()


def test_redacted_pdf_renders_page_previews_up_to_the_page_limit():
    pages = render_pdf(pdf_pages(2))
    assert len(pages) == 2 and all(page.startswith(b"\x89PNG") for page in pages)
    assert Image.open(io.BytesIO(pages[0])).getpixel((10, 10)) == (0, 0, 0)
    assert render_pdf(b"") == []
    with pytest.raises(ValueError, match="has 9 pages"):
        render_pdf(pdf_pages(9))
    with pytest.raises(ValueError, match="could not be read"):
        render_pdf(b"%PDF-1.4 not a document")
    # The upload keeps the redacted PDF; only the previews are skipped.
    assert page_previews({"image": b"", "pdf": pdf_pages(9)}) == (
        [],
        "The redacted PDF has 9 pages. Page previews and visual chat support 8 at most.",
    )
    assert page_previews({"image": b"png", "pdf": b""}) == ([b"png"], "")


def test_every_explorer_operation_is_unique_and_has_a_reviewable_availability():
    assert len(BY_ID) == len(OPERATIONS)
    assert all(item["method"] in {"GET", "POST", "PUT", "PATCH", "DELETE"} for item in OPERATIONS)
    assert all(item["path"].startswith("/") for item in OPERATIONS)
    unverified = public_catalog(set())
    assert {item["availability"] for item in unverified} <= {"catalogued", "not yet verified"}
    discovered = public_catalog({("GET", "/v2/jobs/{job_id}")})
    poll = next(item for item in discovered if item["id"] == "v2.job-poll")
    assert poll["availability"] == "available"
    assert template_matches(poll["path"], "/v2/jobs/job_7f3a9c")
    assert not template_matches(poll["path"], "/v1/documents/jobs/42a75838-068b-4ed5-b975-6258eb25c397")


def test_occupied_port_and_browser_open_failure_are_clear(monkeypatch, capsys):
    class OccupiedSocket:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def setsockopt(self, *args):
            return None

        def bind(self, address):
            raise OSError("occupied")

    monkeypatch.setattr(cli.socket, "socket", lambda *args: OccupiedSocket())
    assert cli.port_available(8765) is False
    monkeypatch.setattr(cli.time, "sleep", lambda _: None)
    monkeypatch.setattr(cli.webbrowser, "open", lambda *args, **kwargs: False)
    cli.open_browser("http://127.0.0.1:8765/")
    assert "Open http://127.0.0.1:8765/ in your browser." in capsys.readouterr().err


def test_unavailable_keyring_falls_back_to_session_only(monkeypatch):
    class UnavailableKeyring:
        priority = 0

    monkeypatch.setattr(credentials.keyring, "get_keyring", lambda: UnavailableKeyring())
    assert credentials.available() is False
    assert credentials.write("shinrai", "secret") is False
    assert credentials.read("shinrai") == ""


def test_vendor_calls_use_the_v1_vendor_prefix():
    from hello_shinrai.client import vendor_path

    assert vendor_path("/language/:analyze-text") == "/v1/azure/language/:analyze-text"
    assert (
        vendor_path("/text/analytics/v3.1/entities/recognition/pii")
        == "/v1/azure/text/analytics/v3.1/entities/recognition/pii"
    )
    assert (
        vendor_path("/v2/projects/hello/locations/global/content:inspect")
        == "/v1/google/v2/projects/hello/locations/global/content:inspect"
    )
    assert vendor_path("/v2/infoTypes") == "/v1/google/v2/infoTypes"
    assert vendor_path("/providers/aws/credentials") == "/v1/aws/providers/aws/credentials"
    assert vendor_path("/v2/detect") == "/v2/detect"
    assert vendor_path("/v1/azure/language/:analyze-text") == "/v1/azure/language/:analyze-text"


def test_v2_mapping_helpers_keep_the_map_restorable():
    from hello_shinrai.client import apply_replacements, known_block, merge_pairs, reversible_pairs, v2_model

    delta = [
        {"original": "Ada Lovelace", "replacement": "Grace Palmer", "reversible": True},
        {"original": "ada@example.org", "replacement": "***", "reversible": False},
        {"original": "Bob", "replacement": "Grace Palmer", "reversible": True},  # would make restore ambiguous
        {"original": "Berlin", "replacement": "Berlin", "reversible": True},
    ]
    assert reversible_pairs(delta) == {"Ada Lovelace": "Grace Palmer"}
    assert reversible_pairs({"Ada": "[PERSON_1]"}) == {}  # only a v2 mapping.delta list is a map
    merged = merge_pairs({"Ada Lovelace": "Grace Palmer"}, [{"original": "Ada Lovelace", "replacement": "Other"}])
    assert merged == {"Ada Lovelace": "Grace Palmer"}
    assert known_block(merged) == {
        "known": [{"original": "Ada Lovelace", "replacement": "Grace Palmer"}],
        "reserved": ["Grace Palmer"],
    }
    entities = [
        {"span": {"start": 0, "end": 3}, "replacement": "Bob"},
        {"span": {"start": 13, "end": 19}, "replacement": "[CITY]"},
        {"span": {"start": 14, "end": 15}, "replacement": "overlap"},  # overlaps: skipped
        {"span": {"start": 4, "end": 9}},  # action keep: no replacement
    ]
    assert apply_replacements("Ada lives in Berlin", entities) == "Bob lives in [CITY]"
    # Code-point offsets: an emoji before a span counts as one position.
    assert apply_replacements("🙂 Ada", [{"span": {"start": 2, "end": 5}, "replacement": "Bob"}]) == "🙂 Bob"
    assert v2_model("shinrai-latest") == v2_model("") == "latest" and v2_model("v1.4") == "v1.4"


def test_v2_trace_view_hides_image_bytes_and_ocr_text_from_safe_export():
    from hello_shinrai.client import trace_view

    body = {
        "results": [
            {
                "input_id": "1",
                "output": {"media_type": "image/png", "data_b64": "QUJD" * 100},
                "media": {"box_unit": "px"},
                "text": "Contact Ada",
                "entities": [{"type": "PERSON", "text": "Ada"}],
            }
        ]
    }
    view = trace_view(body)
    assert view["results"][0]["output"]["data_b64"] == "[400 base64 characters]"
    assert body["results"][0]["output"]["data_b64"].startswith("QUJD")  # the original stays untouched
    safe = sanitize(view)
    assert safe["results"][0]["ocr_text"] == "[omitted from safe export]"
    assert safe["results"][0]["entities"][0]["original"] == "[omitted from safe export]"


def test_document_job_outputs_prefer_the_redacted_pdf_and_the_text_artifact():
    from hello_shinrai.client import RemoteError, Result, document_outputs, refuses_text_artifact

    def result(body):
        return Result(200, body, {}, 0)

    pdf = pdf_pages(1)
    assert document_outputs({"protected": result(pdf), "text": result("[PERSON]")}) == ("[PERSON]", pdf)
    assert document_outputs({"protected": result(pdf)}) == (None, pdf)
    # Older deployments: `protected` is the protected text; a text body in `protected` is never a PDF.
    assert document_outputs({"protected": result("[PERSON]")}) == ("[PERSON]", b"")
    assert document_outputs({"protected": result(b"[PERSON]")}) == ("[PERSON]", b"")
    assert document_outputs({"protected": result("[A]"), "text": result("[B]")}) == ("[B]", b"")
    assert document_outputs({}) == (None, b"")
    refused = {"error": {"details": [{"pointer": "/output/artifacts", "reason": "unknown or repeated artifact"}]}}
    assert refuses_text_artifact(RemoteError("x", status=422, body=refused))
    assert refuses_text_artifact(RemoteError("x", status=501, body=refused))
    assert not refuses_text_artifact(RemoteError("x", status=409, body=refused))
    assert not refuses_text_artifact(
        RemoteError("x", status=422, body={"error": {"details": [{"pointer": "/inputs/0/media_type"}]}})
    )
    assert not refuses_text_artifact(RemoteError("x", status=422, body="Unprocessable"))


def test_catalog_lists_native_v2_first_and_matches_vendor_prefixes():
    assert OPERATIONS[0]["group"] == "ShinrAI native API v2"
    groups = list(dict.fromkeys(item["group"] for item in OPERATIONS))
    assert groups[:2] == ["ShinrAI native API v2", "Azure Language"]
    assert not any(item["path"].startswith("/v1/") or item["id"].startswith("native.") for item in OPERATIONS)
    ids = {item["id"] for item in OPERATIONS if item["group"] == "ShinrAI native API v2"}
    assert {"v2.detect", "v2.protect", "v2.restore", "v2.capabilities", "v2.types", "v2.usage", "v2.job-create"} <= ids
    discovered = public_catalog({("POST", "/v1/google/v2/projects/{project}/locations/{location}/content:inspect")})
    assert next(item for item in discovered if item["id"] == "google.inspect")["availability"] == "available"
