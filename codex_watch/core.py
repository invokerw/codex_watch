"""Bounded body capture and best-effort credential redaction (stdlib only)."""
from __future__ import annotations

import hashlib
import json
import re
import zlib
from typing import Any, Iterable
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

REDACTED = "[REDACTED]"
_SENSITIVE = re.compile(
    r"(?:^|[-_])(?:authorization|cookie|api[-_]?key|access[-_]?token|"
    r"refresh[-_]?token|id[-_]?token|client[-_]?secret|password|passwd|"
    r"secret|token|signature|sig|code)(?:$|[-_])", re.I,
)
_BEARER = re.compile(r"(\b(?:Bearer|Basic)\s+)[A-Za-z0-9._~+/=-]+", re.I)
_API_KEY = re.compile(r"\bsk-[A-Za-z0-9_-]{8,}")
_JWT = re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+")
_KEY_VALUE = re.compile(
    r'''(\b(?:api[-_]?key|access[-_]?token|refresh[-_]?token|client[-_]?secret|'''
    r'''password|passwd|secret|token)\b["']?\s*[:=]\s*)(?:"[^"\n]*"|'[^'\n]*'|[^\s,;&]+)''', re.I,
)


def is_sensitive_name(name: str) -> bool:
    name = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", name).strip().lower()
    return bool(_SENSITIVE.search(name))


def redact_text(text: str) -> str:
    text = _BEARER.sub(lambda m: m[1] + REDACTED, text)
    text = _API_KEY.sub(REDACTED, text)
    text = _JWT.sub(REDACTED, text)
    return _KEY_VALUE.sub(lambda m: m[1] + REDACTED, text)


def redact_url(url: str) -> str:
    try:
        parts = urlsplit(url)
        netloc = parts.netloc.rsplit("@", 1)[-1]
        pairs = [
            (key, REDACTED if is_sensitive_name(key) else redact_text(value))
            for key, value in parse_qsl(parts.query, keep_blank_values=True)
        ]
        return urlunsplit((parts.scheme, netloc, parts.path, urlencode(pairs), ""))
    except ValueError:
        return "[INVALID URL]"


def redact_headers(headers: Iterable[tuple[str, str]]) -> list[list[str]]:
    result = []
    for name, value in headers:
        if is_sensitive_name(name):
            value = REDACTED
        elif name.lower() in {"location", "referer", "referrer"}:
            value = redact_url(value)
        else:
            value = redact_text(value)
        result.append([name, value])
    return result


def redact_json(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: REDACTED if is_sensitive_name(str(key)) else redact_json(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_json(item) for item in value]
    if isinstance(value, str):
        return redact_text(value)
    return value


def host_allowed(host: str, host_filter: str) -> bool:
    patterns = [p.strip().lower().rstrip(".") for p in host_filter.split(",") if p.strip()]
    candidate = host.lower().rstrip(".")
    return not patterns or any(
        candidate == (domain := p.removeprefix("*.").lstrip("."))
        or candidate.endswith("." + domain)
        for p in patterns
    )


class BodyCapture:
    """Hash all wire bytes, retain at most max_bytes+1 decoded bytes.

    Calling feed returns the original bytes unchanged, so this also serves as
    a mitmproxy streaming callback. Never logs undecodable/compressed blobs.
    """

    def __init__(self, *, include_body: bool, max_bytes: int,
                 content_type: str = "", content_encoding: str = "") -> None:
        self.include_body = include_body
        self.limit = max(0, max_bytes)
        self.content_type = content_type.lower().split(";", 1)[0].strip()
        self.content_encoding = content_encoding.lower().strip()
        self.size = 0
        self.digest = hashlib.sha256()
        self.sample = bytearray()
        self.problem: str | None = None
        self.decoder = None
        if self.content_encoding in {"gzip", "x-gzip"}:
            self.decoder = zlib.decompressobj(16 + zlib.MAX_WBITS)
        elif self.content_encoding == "deflate":
            self.decoder = zlib.decompressobj()
        elif self.content_encoding not in {"", "identity"}:
            self.problem = "unsupported_content_encoding"

    def feed(self, chunk: bytes) -> bytes:
        self.size += len(chunk)
        self.digest.update(chunk)
        remaining = self.limit + 1 - len(self.sample)
        if self.include_body and chunk and remaining > 0 and not self.problem:
            try:
                if self.decoder:
                    self.sample.extend(self.decoder.decompress(chunk, remaining))
                    if self.decoder.unused_data:
                        self.problem = "multiple_compressed_members"
                else:
                    self.sample.extend(chunk[:remaining])
            except zlib.error:
                self.problem = "invalid_compressed_body"
        return chunk

    def snapshot(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "bytes": self.size, "sha256": self.digest.hexdigest(), "captured": False,
        }
        if not self.include_body:
            return result
        truncated = len(self.sample) > self.limit
        result["truncated"] = truncated
        if self.problem:
            return {**result, "omitted": self.problem}
        if self.decoder and self.size and not truncated and not self.decoder.eof:
            return {**result, "omitted": "incomplete_compressed_body"}
        raw = bytes(self.sample[:self.limit])
        try:
            text = raw.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            # A limit can split the final UTF-8 character; only discard that tail.
            if truncated and exc.reason == "unexpected end of data" and exc.end == len(raw):
                text = raw[:exc.start].decode("utf-8")
            else:
                return {**result, "omitted": "non_utf8_body"}
        if any(ord(c) < 32 and c not in "\r\n\t" for c in text):
            return {**result, "omitted": "binary_body"}
        if self.content_type == "text/event-stream":
            # Keep complete events only. JSON data is parsed before redaction.
            normalized = text.replace("\r\n", "\n").replace("\r", "\n")
            blocks = normalized.split("\n\n")
            events = []
            for block in blocks[:-1]:
                lines = block.splitlines()
                payload = "\n".join(line[5:].lstrip(" ") for line in lines if line.startswith("data:"))
                metadata = [redact_text(line) for line in lines if not line.startswith("data:")]
                if payload:
                    try:
                        safe = json.dumps(redact_json(json.loads(payload)), ensure_ascii=False)
                    except (ValueError, RecursionError):
                        safe = "[DONE]" if payload == "[DONE]" else "[OMITTED NON-JSON SSE DATA]"
                    metadata.append("data: " + safe)
                events.append("\n".join(metadata))
            result.update(captured=True, encoding="utf-8", text="\n\n".join(events))
            result["incomplete_event_omitted"] = bool(blocks[-1])
            return result
        if "json" in self.content_type or text.lstrip().startswith(("{", "[")):
            if truncated:
                return {**result, "omitted": "truncated_json"}
            try:
                result.update(captured=True, encoding="utf-8", json=redact_json(json.loads(text)))
            except (ValueError, RecursionError):
                result["omitted"] = "invalid_json"
            return result
        if self.content_type == "application/x-www-form-urlencoded":
            if truncated:
                return {**result, "omitted": "truncated_form"}
            result.update(captured=True, encoding="utf-8", form=[
                [k, REDACTED if is_sensitive_name(k) else redact_text(v)]
                for k, v in parse_qsl(text, keep_blank_values=True)
            ])
            return result
        if self.content_type.startswith("multipart/"):
            return {**result, "omitted": "multipart_body"}
        result.update(captured=True, encoding="utf-8", text=redact_text(text))
        return result


def body_snapshot(body: bytes | None, *, include_body: bool, max_bytes: int,
                  content_type: str = "", content_encoding: str = "") -> dict[str, Any]:
    capture = BodyCapture(include_body=include_body, max_bytes=max_bytes,
                          content_type=content_type, content_encoding=content_encoding)
    capture.feed(body or b"")
    return capture.snapshot()


class SSEEventDecoder:
    """Extract complete SSE JSON events without waiting for the HTTP response end."""

    def __init__(self, emit, max_bytes: int = 1048576):
        self.emit = emit
        self.limit = max_bytes
        self.buffer = b""
        self.disabled = False

    def feed(self, data: bytes) -> None:
        if self.disabled:
            return
        self.buffer += data
        while match := re.search(rb"\r\n\r\n|\n\n|\r\r", self.buffer):
            block, self.buffer = self.buffer[:match.start()], self.buffer[match.end():]
            try:
                text = block.decode("utf-8")
                data_text = "\n".join(line[5:].removeprefix(" ") for line in text.splitlines()
                                      if line.startswith("data:"))
                value = {"type": "capture.stream_done"} if data_text == "[DONE]" else json.loads(data_text)
                if isinstance(value, dict):
                    self.emit(redact_json(value))
            except (ValueError, RecursionError):
                pass
        if len(self.buffer) > self.limit:
            self.buffer = b""
            self.disabled = True
