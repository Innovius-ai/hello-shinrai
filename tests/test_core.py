from __future__ import annotations

import io

import pytest
from PIL import Image

from hello_shinrai import cli, credentials
from hello_shinrai.catalog import BY_ID, OPERATIONS, public_catalog, template_matches
from hello_shinrai.client import image_to_pdf, render_pdf, safe_schema_routes
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
            "/v2/projects/{project}/content:inspect": {"post": {}},
            "/console/account": {"get": {}},
            "/ops/health": {"get": {}},
        }
    }
    assert safe_schema_routes(schema) == {("POST", "/v1/analyze"), ("POST", "/v2/projects/{project}/content:inspect")}


def test_image_conversion_produces_renderable_protected_pages():
    raw = io.BytesIO()
    Image.new("RGB", (200, 100), "white").save(raw, format="PNG")
    pdf = image_to_pdf(raw.getvalue())
    pages = render_pdf(pdf)
    assert pdf.startswith(b"%PDF")
    assert len(pages) == 1
    assert pages[0].startswith(b"\x89PNG")


def test_every_explorer_operation_is_unique_and_has_a_reviewable_availability():
    assert len(BY_ID) == len(OPERATIONS)
    assert all(item["method"] in {"GET", "POST", "PUT", "PATCH", "DELETE"} for item in OPERATIONS)
    assert all(item["path"].startswith("/") for item in OPERATIONS)
    unverified = public_catalog(set())
    assert {item["availability"] for item in unverified} <= {"catalogued", "not yet verified"}
    discovered = public_catalog({("GET", "/v1/documents/jobs/{identifier}")})
    poll = next(item for item in discovered if item["id"] == "native.document-poll")
    assert poll["availability"] == "available"
    assert template_matches(poll["path"], "/v1/documents/jobs/42a75838-068b-4ed5-b975-6258eb25c397")


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
    assert vendor_path("/v2/detect") == "/v2/detect" and vendor_path("/v1/analyze") == "/v1/analyze"
    assert vendor_path("/v1/azure/language/:analyze-text") == "/v1/azure/language/:analyze-text"


def test_v2_mapping_helpers_keep_the_map_restorable():
    from hello_shinrai.client import apply_replacements, known_block, merge_pairs, reversible_pairs, v1_model, v2_model

    delta = [
        {"original": "Ada Lovelace", "replacement": "Grace Palmer", "reversible": True},
        {"original": "ada@example.org", "replacement": "***", "reversible": False},
        {"original": "Bob", "replacement": "Grace Palmer", "reversible": True},  # would make restore ambiguous
        {"original": "Berlin", "replacement": "Berlin", "reversible": True},
    ]
    assert reversible_pairs(delta) == {"Ada Lovelace": "Grace Palmer"}
    assert reversible_pairs({"Ada": "[PERSON_1]"}) == {"Ada": "[PERSON_1]"}
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
    assert v1_model("latest") == "shinrai-latest"


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


def test_catalog_lists_native_v2_first_and_matches_vendor_prefixes():
    assert OPERATIONS[0]["group"] == "ShinrAI native API v2"
    groups = list(dict.fromkeys(item["group"] for item in OPERATIONS))
    assert groups[:2] == ["ShinrAI native API v2", "ShinrAI native v1 (legacy)"]
    ids = {item["id"] for item in OPERATIONS if item["group"] == "ShinrAI native API v2"}
    assert {"v2.detect", "v2.protect", "v2.restore", "v2.capabilities", "v2.types", "v2.usage", "v2.job-create"} <= ids
    discovered = public_catalog({("POST", "/v1/google/v2/projects/{project}/locations/{location}/content:inspect")})
    assert next(item for item in discovered if item["id"] == "google.inspect")["availability"] == "available"
