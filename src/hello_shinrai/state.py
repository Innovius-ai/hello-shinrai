from __future__ import annotations

import hashlib
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
    protected_image: bytes = b""
    entities: int | None = None
    note: str = ""


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
    text_mapping: dict[str, str] = field(default_factory=dict)
    llm_model_count: int | None = None
    discovered_routes: set[tuple[str, str]] = field(default_factory=set)
    aws_credentials: dict[str, str] | None = None
    # The connection the issued AWS pair belongs to: (base URL, SHA-256 of the key).
    aws_credentials_owner: tuple[str, str] | None = None
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

    def aws_pair_for(self, base_url: str, key: str) -> dict[str, str] | None:
        """The issued AWS pair if it belongs to this base URL and key."""
        if self.aws_credentials and self.aws_credentials_owner == credential_owner(base_url, key):
            return self.aws_credentials
        return None

    def keep_aws_pair(self, base_url: str, key: str, pair: dict[str, str]) -> None:
        """Cache a pair issued for (base_url, key), unless another connection was made meanwhile."""
        if (self.connection.shinrai_url, self.connection.shinrai_key) == (base_url, key):
            self.aws_credentials, self.aws_credentials_owner = pair, credential_owner(base_url, key)

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


def credential_owner(base_url: str, key: str) -> tuple[str, str]:
    return base_url, hashlib.sha256(key.encode()).hexdigest()
