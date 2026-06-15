"""Central logging configuration.

A redacting filter strips secrets (passwords, tokens, SSH keys) from every log
record so that no sensitive data can ever be written to disk or shown in the UI.
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import re
from typing import List

from .config import get_settings

# Patterns that match "<key>: <secret>" style assignments and standalone keys.
# The capturing groups keep the readable part and replace only the secret value.
_KV_PATTERNS: List[re.Pattern] = [
    re.compile(r'(?i)(password["\']?\s*[:=]\s*)("?)([^"\',\s}]+)'),
    re.compile(r'(?i)(passwd["\']?\s*[:=]\s*)("?)([^"\',\s}]+)'),
    re.compile(r'(?i)(secret["\']?\s*[:=]\s*)("?)([^"\',\s}]+)'),
    re.compile(r'(?i)(token[_-]?secret["\']?\s*[:=]\s*)("?)([^"\',\s}]+)'),
    re.compile(r'(?i)(authorization["\']?\s*[:=]\s*)("?)([^"\',\s}]+)'),
]

# Patterns that should be replaced as a whole.
_WHOLE_PATTERNS: List[re.Pattern] = [
    re.compile(r"PVEAPIToken=\S+"),
    re.compile(r"ssh-(?:rsa|ed25519|dss|ecdsa)\s+[A-Za-z0-9+/=]+"),
    re.compile(r"-----BEGIN [^-]+-----.*?-----END [^-]+-----", re.DOTALL),
]

_REDACTION = "***REDACTED***"


def redact(text: str) -> str:
    """Return ``text`` with any detected secret values replaced."""
    if not text:
        return text
    for pattern in _WHOLE_PATTERNS:
        text = pattern.sub(_REDACTION, text)
    for pattern in _KV_PATTERNS:
        text = pattern.sub(lambda m: f"{m.group(1)}{m.group(2)}{_REDACTION}", text)
    return text


class RedactingFilter(logging.Filter):
    """Logging filter that redacts secrets from the final message."""

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003
        try:
            message = record.getMessage()
        except Exception:  # pragma: no cover - defensive
            message = str(record.msg)
        record.msg = redact(message)
        record.args = ()
        return True


def setup_logging() -> logging.Logger:
    """Configure root logging with console + rotating file handlers."""
    settings = get_settings()
    os.makedirs(settings.log_dir, exist_ok=True)
    log_path = os.path.join(settings.log_dir, settings.log_file)

    level = getattr(logging, settings.log_level.upper(), logging.INFO)
    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
    )
    redactor = RedactingFilter()

    root = logging.getLogger()
    root.setLevel(level)
    root.handlers.clear()

    console = logging.StreamHandler()
    console.setFormatter(formatter)
    console.addFilter(redactor)
    root.addHandler(console)

    file_handler = logging.handlers.RotatingFileHandler(
        log_path, maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)
    file_handler.addFilter(redactor)
    root.addHandler(file_handler)

    # Reduce noise from third party libraries.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("asyncssh").setLevel(logging.WARNING)

    return root


def read_recent_logs(lines: int = 200) -> List[str]:
    """Return the last ``lines`` lines from the application log file."""
    settings = get_settings()
    log_path = os.path.join(settings.log_dir, settings.log_file)
    if not os.path.exists(log_path):
        return []
    # Read the whole file and tail it. Log files are rotated at 5 MB so this is
    # cheap and avoids the complexity of seeking backwards through the file.
    with open(log_path, "r", encoding="utf-8", errors="replace") as handle:
        all_lines = handle.readlines()
    return [line.rstrip("\n") for line in all_lines[-lines:]]
