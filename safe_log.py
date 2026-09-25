"""The desktop build's log: bounded, and never holding a key.

A windowed build has no console, so play.py points stdout/stderr at
logs/somewhere.log. That file used to be opened for append and never trimmed,
and whatever a traceback or a provider error echoed went into it verbatim — an
error body that quotes the request URL carries `?key=AIza…`. The bug button
attaches this log, so a key in it is a key in a bug report.
(docs/plans/DISTRIBUTION_MVP_PLAN.md, M1.)

So every write is redacted first, and the file rolls over at 10 MB, keeping 3.
Redaction is per write: a key split across two writes would get through, which
print() and tracebacks do not do.
"""
from __future__ import annotations

import io
import os
import re
import threading
from pathlib import Path
from typing import Iterable

MASK = "[redacted]"

# Shapes of the secrets this game handles. Specific prefixes first; the last
# catches `key=` / `api_key: ` / `Bearer ` followed by a long token.
_PATTERNS = [
    re.compile(r"AIza[0-9A-Za-z_\-]{30,}"),                      # Google / Gemini
    re.compile(r"sk-(?:proj-|ant-|svcacct-)?[A-Za-z0-9_\-]{20,}"),  # OpenAI / Anthropic
    re.compile(r"\b(?:sk|rk|pk)_(?:live|test)_[0-9A-Za-z]{16,}"),  # Stripe
    re.compile(r"\bwhsec_[0-9A-Za-z]{16,}"),                      # Stripe webhook
    re.compile(r"\bsk_[0-9a-f]{40,}\b"),                          # ElevenLabs
    re.compile(r"\br8_[0-9A-Za-z]{20,}"),                         # Replicate
    re.compile(r"\bxai-[0-9A-Za-z]{20,}"),
    re.compile(r"(?i)((?:api[_-]?key|x-goog-api-key|xi-api-key|token|secret|authorization)"
               r"[\"']?\s*[:=]\s*[\"']?(?:Bearer\s+)?)[A-Za-z0-9_\-\.]{20,}"),
    re.compile(r"(?i)(\bBearer\s+)[A-Za-z0-9_\-\.]{20,}"),
]

_extra: set = set()


def add_secret(value: str) -> None:
    """Redact this exact string too (the launch token)."""
    if value and len(value) >= 8:
        _extra.add(value)


def redact(text: str) -> str:
    if not text:
        return text
    for s in _extra:
        if s in text:
            text = text.replace(s, MASK)
    for p in _PATTERNS:
        if p.groups:
            text = p.sub(lambda m: m.group(1) + MASK, text)
        else:
            text = p.sub(MASK, text)
    return text


class RotatingRedactingStream(io.TextIOBase):
    """A text stream for sys.stdout/sys.stderr that redacts and rotates."""

    def __init__(self, path: Path, max_bytes: int = 10 * 1024 * 1024, backups: int = 3):
        self.path = Path(path)
        self.max_bytes = max_bytes
        self.backups = backups
        self._lock = threading.Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(self.path, "a", encoding="utf-8", buffering=1)

    def _rotate(self) -> None:
        self._fh.close()
        for i in range(self.backups - 1, 0, -1):
            src = self.path.with_name(f"{self.path.name}.{i}")
            if src.exists():
                os.replace(src, self.path.with_name(f"{self.path.name}.{i + 1}"))
        if self.path.exists():
            os.replace(self.path, self.path.with_name(f"{self.path.name}.1"))
        self._fh = open(self.path, "a", encoding="utf-8", buffering=1)

    def write(self, s: str) -> int:
        n = len(s)
        clean = redact(s)
        with self._lock:
            try:
                if self._fh.tell() + len(clean.encode("utf-8", "replace")) > self.max_bytes:
                    self._rotate()
                self._fh.write(clean)
            except (OSError, ValueError):
                pass
        return n

    def flush(self) -> None:
        with self._lock:
            try:
                self._fh.flush()
            except (OSError, ValueError):
                pass

    def writable(self) -> bool:
        return True

    @property
    def encoding(self) -> str:  # type: ignore[override]
        return "utf-8"

    def isatty(self) -> bool:
        return False


def redact_lines(lines: Iterable[str]) -> list:
    return [redact(line) for line in lines]
