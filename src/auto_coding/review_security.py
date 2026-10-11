"""Conservative review-data screening; not a guarantee of secret removal."""

from __future__ import annotations

import os
from pathlib import PurePosixPath
import re
from typing import Any


class SensitiveEvidenceError(ValueError):
    """Evidence cannot be safely disclosed."""


_PATTERNS = (
    r"\b(?:gsk_|sk-|gh[pousr]_|github_pat_)[A-Za-z0-9_-]{12,}",
    r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b",
    r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b",
    r"(?i)\b(?:bearer|basic)\s+[A-Za-z0-9+/_.=-]{8,}",
    r"(?i)(?:https?|ssh)://[^\s/@:]+:[^\s/@]+@",
    r"""(?i)\b(?:[a-z0-9]+_)*(?:api[_-]?key|access[_-]?token|auth[_-]?token|token|password|passwd|secret|secret_access_key|client_secret)\b["']?\s*[:=]\s*["']?[^"'\s,;}]+""",
)
_SECRETS = tuple(re.compile(pattern) for pattern in _PATTERNS)


def redact(text: str) -> tuple[str, bool]:
    """Return safe text and whether evidence changed. Never log input."""
    changed = False
    key = os.environ.get("GROQ_API_KEY", "")
    if key and key in text:
        text = text.replace(key, "[REDACTED]")
        changed = True
    # Incomplete private keys/certificates cannot be reliably bounded.
    if re.search(r"-----BEGIN [^-]*(?:PRIVATE KEY|CERTIFICATE)-----", text):
        return "[WITHHELD: sensitive key material]", True
    for pattern in _SECRETS:
        text, count = pattern.subn("[REDACTED]", text)
        changed = changed or bool(count)
    return text, changed


def sanitize(value: Any) -> tuple[Any, bool]:
    """Recursively screen JSON-compatible objects, including their keys."""
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, (tuple, list)):
        values = [sanitize(item) for item in value]
        return [item for item, _ in values], any(flag for _, flag in values)
    if isinstance(value, dict):
        result, changed = {}, False
        for key, item in value.items():
            safe_key, key_changed = redact(str(key))
            safe_item, item_changed = sanitize(item)
            result[safe_key] = safe_item
            changed |= key_changed or item_changed
        return result, changed
    return value, False


def validate_review_path(path: str) -> None:
    if not isinstance(path, str):
        raise SensitiveEvidenceError("Invalid changed-file path type")
    normalized = path.replace("\\", "/")
    parts = normalized.split("/")
    if (not normalized or normalized.startswith(("/", "-"))
            or any(c in normalized for c in ':*?[]<>"|{}\x00')
            or any(ord(c) < 32 for c in normalized)
            or any(part in ("", ".", "..") or part.endswith((" ", ".")) for part in parts)):
        raise SensitiveEvidenceError("Unsafe changed-file path")
    forbidden = {".git", ".codex", ".aws", ".ssh", ".auth", "credentials", "secrets",
                 "browser-profile", "browser_profiles", "browser-profiles", "review-transcripts",
                 "review-history", "runtime"}
    names = {"auth.json", "credentials.json", "credentials.yml", "credentials.yaml",
             "cookies.json", "cookies", "login data", "storage-state.json",
             "storage_state.json", "token.json", "tokens.json", "session.json", "sessions.json",
             "config.toml", ".netrc", "_netrc", ".npmrc", ".pypirc", "id_rsa", "id_ed25519"}
    name = parts[-1].casefold()
    if (set(part.casefold() for part in parts) & forbidden or name in names
            or name.startswith(".env") or PurePosixPath(name).suffix in {
                ".pem", ".key", ".p12", ".pfx", ".secret", ".sqlite", ".sqlite3", ".db",
            }):
        raise SensitiveEvidenceError("Sensitive changed-file path")
    if any(re.fullmatch(r"(?i)(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?", part) for part in parts):
        raise SensitiveEvidenceError("Non-portable changed-file path")
