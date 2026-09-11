from __future__ import annotations

try:
    import keyring
    from keyring.errors import KeyringError
except ImportError:  # pragma: no cover - keyring is a runtime dependency
    keyring = None
    KeyringError = Exception

SERVICE = "hello-shinrai"
NAMES = {"shinrai", "llm", "azure"}


def available() -> bool:
    if keyring is None:
        return False
    try:
        return keyring.get_keyring().priority > 0
    except (KeyringError, RuntimeError, AttributeError):
        return False


def read(name: str) -> str:
    if name not in NAMES or not available():
        return ""
    try:
        return keyring.get_password(SERVICE, name) or ""
    except (KeyringError, RuntimeError):
        return ""


def write(name: str, value: str) -> bool:
    if name not in NAMES or not value or not available():
        return False
    try:
        keyring.set_password(SERVICE, name, value)
        return True
    except (KeyringError, RuntimeError):
        return False


def forget():
    if keyring is None:
        return
    for name in NAMES:
        try:
            keyring.delete_password(SERVICE, name)
        except (KeyringError, RuntimeError):
            pass
