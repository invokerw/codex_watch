"""mitmproxy addon: JSONL request/response/error/WebSocket events."""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from mitmproxy import ctx, exceptions, http

from codex_watch.core import BodyCapture, SSEEventDecoder, body_snapshot, host_allowed, redact_headers, redact_text, redact_url
from codex_watch.platform_support import restrict_file

DEFAULT_HOST_FILTER = "api.openai.com,chatgpt.com"


def timestamp(value: float | None = None) -> str:
    return (datetime.fromtimestamp(value, timezone.utc) if value is not None
            else datetime.now(timezone.utc)).isoformat()


class CodexCapture:
    def __init__(self) -> None:
        self.responses: dict[str, BodyCapture] = {}
        self.output: Path | None = None

    def load(self, loader: Any) -> None:
        loader.add_option("capture_output", str, "./captures/codex.jsonl", "Append JSONL events here.")
        loader.add_option("capture_hosts", str, DEFAULT_HOST_FILTER, "Domain list; empty captures all.")
        loader.add_option("capture_body", bool, False, "Save redacted, bounded body previews.")
        loader.add_option("capture_max_bytes", int, 1048576, "Maximum decoded body preview bytes.")

    def configure(self, updated: set[str]) -> None:
        if ctx.options.capture_max_bytes < 0:
            raise exceptions.OptionsError("capture_max_bytes must be non-negative")
        if "capture_output" in updated or self.output is None:
            output = Path(ctx.options.capture_output).expanduser()
            try:
                output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                # Restrict capture files even if they already exist.
                fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
                try:
                    restrict_file(fd)
                finally:
                    os.close(fd)
            except OSError as exc:
                raise exceptions.OptionsError(f"Cannot open capture_output: {exc}") from exc
            self.output = output

    def _capture(self, message: http.Message) -> BodyCapture:
        return BodyCapture(
            include_body=ctx.options.capture_body, max_bytes=ctx.options.capture_max_bytes,
            content_type=message.headers.get("content-type", ""),
            content_encoding=message.headers.get("content-encoding", ""),
        )

    def _base(self, flow: http.HTTPFlow, event: str) -> dict[str, Any]:
        return {
            "schema_version": 2, "event": event, "flow_id": flow.id,
            "captured_at": timestamp(), "method": flow.request.method,
            "url": redact_url(flow.request.pretty_url),
        }

    def request(self, flow: http.HTTPFlow) -> None:
        allowed = host_allowed(flow.request.host, ctx.options.capture_hosts)
        flow.metadata["codex_capture_allowed"] = allowed
        if not allowed:
            return
        capture = self._capture(flow.request)
        capture.feed(flow.request.raw_content or b"")
        record = self._base(flow, "request")
        record["request"] = {
            "timestamp": timestamp(flow.request.timestamp_start),
            "http_version": flow.request.http_version,
            "headers": redact_headers(flow.request.headers.items(multi=True)),
            "body": capture.snapshot(),
        }
        self._write(record)

    def responseheaders(self, flow: http.HTTPFlow) -> None:
        assert flow.response is not None
        if flow.response.status_code == 101:
            return  # WebSocket has its own message hooks.
        if flow.metadata.get("codex_capture_allowed"):
            capture = self._capture(flow.response)
            self.responses[flow.id] = capture
            if ctx.options.capture_body and capture.content_type == "text/event-stream":
                def emit(payload):
                    index = flow.metadata.get("codex_capture_sse_index", 0)
                    flow.metadata["codex_capture_sse_index"] = index + 1
                    record = self._base(flow, "sse_event")
                    record.update(index=index, body={"captured": True, "json": payload},
                                  response_headers=redact_headers(flow.response.headers.items(multi=True)))
                    self._write(record)

                decoder = SSEEventDecoder(emit, ctx.options.capture_max_bytes)

                def stream(chunk):
                    start = len(capture.sample)
                    result = capture.feed(chunk)
                    if not capture.problem:
                        decoder.feed(bytes(capture.sample[start:capture.limit]))
                    return result

                flow.response.stream = stream
            else:
                flow.response.stream = capture.feed
        else:
            flow.response.stream = True

    def response(self, flow: http.HTTPFlow) -> None:
        if flow.metadata.get("codex_capture_allowed"):
            self._write(self._response_record(flow, "response"))

    def _response_record(self, flow: http.HTTPFlow, event: str) -> dict[str, Any]:
        record = self._base(flow, event)
        capture = self.responses.pop(flow.id, None)
        response = flow.response
        if response is not None:
            if capture is None:
                capture = self._capture(response)
                capture.feed(response.raw_content or b"")
            record["response"] = {
                "status_code": response.status_code, "http_version": response.http_version,
                "headers": redact_headers(response.headers.items(multi=True)),
                "body": capture.snapshot(),
            }
            end = response.timestamp_end
            if end is not None and flow.request.timestamp_start is not None:
                record["duration_ms"] = round((end - flow.request.timestamp_start) * 1000, 3)
        else:
            record["response"] = None
        return record

    def error(self, flow: http.HTTPFlow) -> None:
        if flow.metadata.get("codex_capture_allowed"):
            record = self._response_record(flow, "error")
            record["error"] = redact_text(str(flow.error))
            self._write(record)
        else:
            self.responses.pop(flow.id, None)

    def websocket_message(self, flow: http.HTTPFlow) -> None:
        if not flow.metadata.get("codex_capture_allowed") or flow.websocket is None:
            return
        message = flow.websocket.messages[-1]
        index = flow.metadata.get("codex_capture_ws_index", 0)
        flow.metadata["codex_capture_ws_index"] = index + 1
        record = self._base(flow, "websocket_message")
        record.update(
            index=index, direction="client_to_server" if message.from_client else "server_to_client",
            timestamp=timestamp(message.timestamp), is_text=message.is_text,
            body=body_snapshot(message.content, include_body=ctx.options.capture_body and message.is_text,
                               max_bytes=ctx.options.capture_max_bytes),
        )
        self._write(record)

    def websocket_end(self, flow: http.HTTPFlow) -> None:
        if flow.metadata.get("codex_capture_allowed") and flow.websocket is not None:
            record = self._base(flow, "websocket_end")
            record.update(close_code=flow.websocket.close_code,
                          close_reason=redact_text(flow.websocket.close_reason or ""))
            self._write(record)

    def _write(self, record: dict[str, Any]) -> None:
        assert self.output is not None
        try:
            with self.output.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
        except OSError as exc:
            logging.error("Capture write failed (%s): %s", self.output, exc)


addons = [CodexCapture()]
