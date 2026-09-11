from __future__ import annotations

import os
import secrets
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from .security import sanitize


@dataclass
class Connection:
    shinrai_url: str = "https://api.shinrai.innovius.io"
    shinrai_key: str = ""
    llm_provider: str = "innovius"
    llm_models_url: str = "https://api.innovius.ai/v1/models"
    llm_inference_url: str = "https://api.innovius.ai/v1/chat/completions"
    llm_protocol: str = "chat"
    llm_key: str = ""
    llm_model: str = ""
    azure_url: str = ""
    azure_key: str = ""


@dataclass
class Attachment:
    id: str
    name: str
    media_type: str
    protected_text: str
    mapping: dict[str, str]
    protected_pdf: bytes
    page_images: list[bytes]
    job_id: str
    cleanup: str
    trace_id: str


@dataclass
class Chat:
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    messages: list[dict[str, str]] = field(default_factory=list)
    mapping: dict[str, str] = field(default_factory=dict)


@dataclass
class RuntimeState:
    token: str = field(default_factory=lambda: secrets.token_urlsafe(32))
    connection: Connection = field(default_factory=Connection)
    capabilities: dict[str, Any] | None = None
    usage: dict[str, Any] | None = None
    llm_model_count: int | None = None
    discovered_routes: set[tuple[str, str]] = field(default_factory=set)
    aws_credentials: dict[str, str] | None = None
    traces: list[dict[str, Any]] = field(default_factory=list)
    attachments: dict[str, Attachment] = field(default_factory=dict)
    chat: Chat = field(default_factory=Chat)
    started_at: float = field(default_factory=time.time)

    def __post_init__(self):
        self.connection.shinrai_key = os.getenv("SHINRAI_API_KEY", "")
        self.connection.shinrai_url = os.getenv("SHINRAI_BASE_URL", self.connection.shinrai_url)
        self.connection.llm_key = os.getenv("HELLO_SHINRAI_LLM_API_KEY", "")
        self.connection.llm_models_url = os.getenv("HELLO_SHINRAI_LLM_MODELS_URL", self.connection.llm_models_url)
        self.connection.llm_inference_url = os.getenv(
            "HELLO_SHINRAI_LLM_INFERENCE_URL", self.connection.llm_inference_url
        )
        self.connection.llm_model = os.getenv("HELLO_SHINRAI_LLM_MODEL", "")
        self.connection.azure_key = os.getenv("AZURE_LANGUAGE_KEY", "")
        self.connection.azure_url = os.getenv("AZURE_LANGUAGE_ENDPOINT", "")

    def trace(self, operation: str, **values: Any) -> dict[str, Any]:
        item = {
            "id": str(uuid.uuid4()),
            "time": time.time(),
            "operation": operation,
            "status": values.pop("status", "started"),
            **values,
        }
        self.traces.insert(0, item)
        del self.traces[100:]
        return item

    def safe_traces(self, include_sensitive: bool = False) -> list[dict[str, Any]]:
        return sanitize(self.traces, include_sensitive=include_sensitive)
