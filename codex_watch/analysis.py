"""Offline analysis of capture JSONL/JSON: retain metadata, never prompt bodies."""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import tempfile
from typing import Any, Iterable
from urllib.parse import urlsplit

from codex_watch.core import redact_text, redact_url

TERMINAL = {"response.completed", "response.failed", "response.incomplete", "response.cancelled"}
NOTES = [
    "模型按完整标识精确比较；不同别名或版本名也会标记差异，不代表已证明服务端更换权重。",
    "enabled 是机制配置，safety_buffering 是事件状态，字段缺失按未知处理；false 不代表没有安全检查。",
    "等待提示是分析器对抓包状态的解释；网络日志不能确认客户端界面是否实际显示提示或用户是否点击重试。",
    "faster_model/retry_model 是候选信息。候选重试要求相同 thread、相同输入、请求模型改为先前候选，仍不是用户点击重试的确证。跨 thread 的重试无法据此判断。",
    "耗时是请求至完成响应的时间，不等同于安全检查耗时；SSE 子事件共用该日志行的记录时间。",
    "输入文件按参数或文件名顺序读取；跨文件分割的同一连接需保持捕获顺序。每条证据均保留文件和位置。",
]


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":")).encode()).hexdigest()


def stamp(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except (AttributeError, TypeError, ValueError):
        return None


def text_value(value: Any) -> str | None:
    return redact_text(value[:1000]) if isinstance(value, str) and value else None


def headers(value: Any) -> dict[str, str]:
    pairs = value.items() if isinstance(value, dict) else value if isinstance(value, list) else []
    return {pair[0].lower(): str(pair[1]) for pair in pairs
            if isinstance(pair, (list, tuple)) and len(pair) == 2 and isinstance(pair[0], str)}


def chat_endpoint(url: str) -> bool:
    try:
        path = urlsplit(url).path.rstrip("/")
        return path.endswith(("/responses", "/chat/completions"))
    except ValueError:
        return False


def source_label(source: dict) -> str:
    return f"{source['file']}:{source['line']}" if source.get("line") else f"{source['file']} [item {source.get('item')}]"


def discover_inputs(paths: Iterable[Path]) -> list[Path]:
    result = []
    for path in paths:
        path = path.expanduser().resolve()
        if path.is_dir():
            # JSONL first: duplicate pretty exports then retain JSONL line references.
            files = sorted((p for p in path.iterdir() if p.is_file() and
                            (p.suffix.lower() in {".jsonl", ".json"} or p.name == "dump")),
                           key=lambda p: (p.suffix != ".jsonl", p.name))
        elif path.is_file():
            files = [path]
        else:
            raise ValueError(f"输入路径不存在: {path}")
        for file in files:
            if file not in result:
                result.append(file)
    if not result:
        raise ValueError("没有找到 JSONL / JSON 抓包文件")
    return result


def read_records(path: Path, warnings: list[dict]):
    if path.suffix.lower() == ".jsonl":
        with path.open(encoding="utf-8-sig") as handle:
            for line, raw in enumerate(handle, 1):
                if not raw.strip():
                    continue
                source = {"file": str(path), "line": line}
                try:
                    yield source, json.loads(raw)
                except ValueError:
                    warnings.append({"source": source, "code": "invalid_json_line"})
        return
    raw = path.read_text(encoding="utf-8-sig")
    decoder, offset, previous, line = json.JSONDecoder(), 0, 0, 1
    while offset < len(raw):
        while offset < len(raw) and raw[offset].isspace():
            offset += 1
        if offset == len(raw):
            break
        line += raw.count("\n", previous, offset)
        source = {"file": str(path), "line": line}
        try:
            value, end = decoder.raw_decode(raw, offset)
        except ValueError:
            warnings.append({"source": source, "code": "invalid_json_document"})
            break
        if isinstance(value, list):
            for index, record in enumerate(value, 1):
                yield {"file": str(path), "line": None, "item": index}, record
        else:
            yield source, value
        previous, offset = offset, end


def payloads(body: Any) -> tuple[list[dict], list[str]]:
    if not isinstance(body, dict):
        return [], ["body_missing"]
    issues = []
    if body.get("omitted"):
        issues.append(str(body["omitted"]))
    if body.get("truncated"):
        issues.append("body_truncated")
    if body.get("incomplete_event_omitted"):
        issues.append("incomplete_sse_event_omitted")
    if isinstance(body.get("json"), dict):
        return [body["json"]], issues
    raw = body.get("text")
    if not isinstance(raw, str):
        return [], issues or ["body_not_captured"]
    if not raw.strip():
        return [], issues
    try:
        parsed = json.loads(raw)
        return ([parsed] if isinstance(parsed, dict) else []), issues
    except ValueError:
        pass
    result = []
    # Both original SSE and the addon's normalized SSE (no trailing blank line).
    for block in raw.replace("\r\n", "\n").replace("\r", "\n").split("\n\n"):
        data = "\n".join(line[5:].lstrip(" ") for line in block.splitlines() if line.startswith("data:"))
        if data == "[DONE]":
            result.append({"type": "capture.stream_done"})
            continue
        if not data:
            continue
        try:
            value = json.loads(data)
            if isinstance(value, dict):
                result.append(value)
        except ValueError:
            issues.append("unreadable_sse_data")
    return result, issues or ([] if result else ["unreadable_body"])


class Analyzer:
    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.active: dict[str, list[dict]] = defaultdict(list)
        self.by_response: dict[tuple[str, str], dict] = {}
        self.connection_headers: dict[str, tuple[dict, dict]] = {}
        self.http_calls: dict[str, dict] = {}
        self.warnings: list[dict] = []
        self.events = Counter()
        self.unique_records = 0
        self.duplicate_records = 0
        self.ignored_records = 0
        self.seen: set[str] = set()

    def warn(self, source: dict, code: str) -> None:
        self.warnings.append({"source": source, "code": code})

    def new_call(self, row: dict, source: dict, payload: dict, issues: list[str], transport: str) -> dict:
        metadata = payload.get("client_metadata") or {}
        turn = metadata.get("x-codex-turn-metadata", {}) if isinstance(metadata, dict) else {}
        if isinstance(turn, str):
            try:
                turn = json.loads(turn)
            except ValueError:
                turn = {}
        if not isinstance(turn, dict):
            turn = {}
        if not isinstance(metadata, dict):
            metadata = {}
        fmt = (payload.get("text") or {}).get("format", {})
        schema = fmt.get("schema", {}) if isinstance(fmt, dict) else {}
        title = isinstance(schema, dict) and set(schema.get("properties", {})) == {"title"}
        prewarm = payload.get("generate") is False or turn.get("request_kind") == "prewarm"
        kind = "prewarm" if prewarm else "title_generation" if title else "conversation"
        call = {
            "id": f"call-{len(self.calls) + 1:04d}", "flow_id": row["flow_id"],
            "transport": transport, "url": redact_url(row.get("url", "")),
            "request_source": source, "requested_at": row.get("captured_at"),
            "request_kind": text_value(turn.get("request_kind")), "purpose": kind,
            "purpose_inferred": title and not prewarm,
            "thread_id": text_value(turn.get("thread_id") or metadata.get("thread_id")),
            "thread_source": text_value(turn.get("thread_source")),
            "turn_id": text_value(turn.get("turn_id") or metadata.get("turn_id")),
            "request_model": text_value(payload.get("model")),
            "request_effort": text_value((payload.get("reasoning") or {}).get("effort")),
            "response_models": [], "response_efforts": [], "response_id": None,
            "model_observations": [], "status": "pending", "completed_at": None,
            "completion_source": None, "duration_ms": None,
            "safety": {"enabled": None, "configuration_evidence": [], "faster_models": [],
                       "state_events": [], "details": [], "ui_display_confirmed": None},
            "retry": {"assessment": "not_observed", "previous_call_id": None,
                      "evidence": [], "ui_action_confirmed": None},
            "issues": list(dict.fromkeys(issues)),
            "_input_fingerprint": digest(payload.get("input", payload.get("messages")))
                                  if "input" in payload or "messages" in payload else None,
            "_previous_response_id": payload.get("previous_response_id"),
        }
        self.calls.append(call)
        self.active[row["flow_id"]].append(call)
        if transport == "http":
            self.http_calls[row["flow_id"]] = call
        if transport == "websocket" and row["flow_id"] in self.connection_headers:
            hdr, ref = self.connection_headers[row["flow_id"]]
            self.apply_headers(call, hdr, ref, "connection")
        return call

    def apply_headers(self, call: dict, hdr: dict, source: dict, scope: str = "request") -> None:
        values = {}
        enabled = hdr.get("x-codex-safety-buffering-enabled", "").strip().lower()
        if enabled in {"true", "false"}:
            call["safety"]["enabled"] = enabled == "true"
            values["enabled"] = enabled == "true"
        faster = text_value(hdr.get("x-codex-safety-buffering-faster-model"))
        if faster:
            values["faster_model"] = faster
            if faster not in call["safety"]["faster_models"]:
                call["safety"]["faster_models"].append(faster)
        if values:
            call["safety"]["configuration_evidence"].append({"source": source, "scope": scope, **values})

    def choose(self, flow: str, payload: dict, source: dict) -> dict | None:
        response = payload.get("response")
        rid = response.get("id") if isinstance(response, dict) else payload.get("response_id")
        if not rid and (payload.get("object", "").startswith("chat.completion") or payload.get("object") == "response"):
            rid = payload.get("id")
        if rid and (flow, rid) in self.by_response:
            return self.by_response[flow, rid]
        active = self.active[flow]
        candidates = [c for c in active if c["response_id"] is None] if rid else active
        if len(candidates) == 1:
            call = candidates[0]
            if rid:
                call["response_id"] = rid
                self.by_response[flow, rid] = call
            return call
        self.warn(source, "ambiguous_response_association" if candidates else "orphan_response_event")
        return None

    def finish(self, call: dict, source: dict, time: str | None, status: str) -> None:
        call.update(status=status, completed_at=time, completion_source=source)
        if call in self.active[call["flow_id"]]:
            self.active[call["flow_id"]].remove(call)

    def observe(self, call: dict, payload: dict, source: dict, time: str | None) -> None:
        kind = payload.get("type", payload.get("object", "response"))
        if kind == "codex.response.metadata":
            self.apply_headers(call, headers(payload.get("headers")), source)
        response = payload.get("response") if isinstance(payload.get("response"), dict) else payload
        model = text_value(response.get("model"))
        if model:
            if model not in call["response_models"]:
                call["response_models"].append(model)
            call["model_observations"].append({"source": source, "event": kind, "model": model})
        rid = response.get("id")
        if rid and (isinstance(payload.get("response"), dict) or response.get("object") in {"response", "chat.completion", "chat.completion.chunk"}):
            call["response_id"] = rid
            self.by_response[call["flow_id"], rid] = call
        effort = text_value((response.get("reasoning") or {}).get("effort"))
        if effort and effort not in call["response_efforts"]:
            call["response_efforts"].append(effort)
        if "safety_buffering" in payload:
            value = payload["safety_buffering"]
            if isinstance(value, bool):
                call["safety"]["state_events"].append({"source": source, "time": time, "active": value})
            elif isinstance(value, dict):
                detail = {k: v for k, v in value.items() if k in {"use_cases", "reasons", "retry_model", "show_buffering_ui"}}
                call["safety"]["details"].append({"source": source, **detail})
                faster = text_value(value.get("retry_model"))
                if faster and faster not in call["safety"]["faster_models"]:
                    call["safety"]["faster_models"].append(faster)
                if isinstance(value.get("show_buffering_ui"), bool):
                    call["safety"]["state_events"].append({"source": source, "time": time,
                                                          "active": value["show_buffering_ui"]})
        if kind in TERMINAL:
            self.finish(call, source, time, kind.removeprefix("response."))
        elif kind in {"error", "response.error"}:
            self.finish(call, source, time, "failed")
        elif kind == "capture.stream_done" and call["status"] == "pending":
            self.finish(call, source, time, "completed")
        elif response.get("object") == "response" and response.get("status") in {"completed", "failed", "incomplete", "cancelled"}:
            self.finish(call, source, time, response["status"])

    def accept(self, source: dict, row: Any) -> None:
        if not isinstance(row, dict) or not row.get("flow_id"):
            self.warn(source, "not_capture_record")
            return
        fingerprint = digest(row)
        if fingerprint in self.seen:
            self.duplicate_records += 1
            return
        self.seen.add(fingerprint)
        self.unique_records += 1
        event = row.get("event", "legacy")
        self.events[event] += 1
        row = dict(row)
        row["url"] = row.get("url") or (row.get("request") or {}).get("url", "")
        if not chat_endpoint(row["url"]):
            self.ignored_records += 1
            return
        if event == "legacy":
            self.process({**row, "event": "request", "method": row.get("request", {}).get("method")}, source)
            if row.get("response") or row.get("error"):
                self.process({**row, "event": "error" if row.get("error") else "response"}, source)
        else:
            self.process(row, source)

    def process(self, row: dict, source: dict) -> None:
        event, flow, time = row["event"], row["flow_id"], row.get("captured_at")
        if event == "request":
            if row.get("method") == "GET":
                return  # A WebSocket handshake is not a model call.
            values, issues = payloads((row.get("request") or {}).get("body"))
            self.new_call(row, source, values[0] if values else {}, issues, "http")
        elif event == "websocket_message":
            values, issues = payloads(row.get("body"))
            if row.get("direction") == "client_to_server":
                if not values:
                    self.new_call(row, source, {}, issues, "websocket")
                for value in values:
                    if value.get("type") == "response.create":
                        request = value.get("response") if isinstance(value.get("response"), dict) else value
                        self.new_call(row, source, request, issues, "websocket")
            else:
                if issues:
                    for call in self.active[flow]:
                        call["issues"].extend(issues)
                for value in values:
                    if not (value.get("response") or value.get("type") in {"codex.response.metadata", "error", "response.error"}
                            or "safety_buffering" in value or value.get("model")
                            or value.get("type", "").startswith("response.output_")
                            or value.get("type") == "response.content_part.done"):
                        continue
                    call = self.choose(flow, value, source)
                    if call is not None:
                        self.observe(call, value, source, time)
        elif event == "sse_event":
            call = self.http_calls.get(flow)
            if call is None:
                self.warn(source, "orphan_sse_event")
                return
            call["_stream_events_seen"] = True
            self.apply_headers(call, headers(row.get("response_headers")), source)
            values, issues = payloads(row.get("body"))
            call["issues"].extend(issues)
            for value in values:
                self.observe(call, value, source, time)
        elif event in {"response", "error"}:
            response = row.get("response") or {}
            hdr = headers(response.get("headers"))
            if response.get("status_code") == 101:
                self.connection_headers[flow] = (hdr, source)
                return
            active = self.active[flow]
            call = self.http_calls.get(flow) or (active[0] if len(active) == 1 else None)
            if call is None:
                if event == "response":
                    self.warn(source, "orphan_http_response")
                return
            call["http_status"] = response.get("status_code")
            self.apply_headers(call, hdr, source)
            values, issues = payloads(response.get("body"))
            call["issues"].extend(issues)
            for index, value in enumerate([] if call.get("_stream_events_seen") else values, 1):
                ref = {**source, "payload_index": index}
                self.observe(call, value, ref, time)
            status = response.get("status_code") or 0
            if event == "error" or status >= 400:
                self.finish(call, source, time, "failed")
            elif call["status"] == "pending":
                # HTTP end doesn't prove a Responses SSE stream completed.
                is_sse = "text/event-stream" in hdr.get("content-type", "")
                self.finish(call, source, time, "incomplete" if is_sse else "http_finished")
        elif event == "websocket_end":
            for call in list(self.active[flow]):
                self.finish(call, source, time, "connection_closed_without_completion")

    def report(self, files: list[Path], exclude_prewarm: bool = False) -> dict:
        previous_by_thread = {}
        for call in sorted(self.calls, key=lambda c: (stamp(c["requested_at"]) or datetime.min.replace(tzinfo=timezone.utc), c["id"])):
            model, observed = call["request_model"], call["response_models"]
            call["model_match"] = "unknown" if not model or not observed else "match" if all(m == model for m in observed) else "mismatch"
            start, end = stamp(call["requested_at"]), stamp(call["completed_at"])
            if start and end:
                call["duration_ms"] = round((end - start).total_seconds() * 1000, 3)
            call["issues"] = list(dict.fromkeys(call["issues"]))
            call["response_model"] = observed[-1] if observed else None
            call["comparison_scope"] = "captured_model_fields"
            safety = call["safety"]
            safety["true_events"] = sum(e["active"] is True for e in safety["state_events"])
            safety["false_events"] = sum(e["active"] is False for e in safety["state_events"])
            safety["waiting_state"] = "observed" if safety["true_events"] else "not_observed" if safety["false_events"] else "unknown"
            safety["waiting_hint"] = {"observed": "已报告安全缓冲/等待状态；不能确认界面已显示提示。",
                                      "not_observed": "已捕获状态均为 false；不等于未启用安全检查。",
                                      "unknown": "未捕获明确等待状态，无法判断。"}[safety["waiting_state"]]
            thread = call["thread_id"]
            if thread and call["purpose"] != "prewarm":
                previous = previous_by_thread.get(thread)
                if previous and model and previous["request_model"] and model != previous["request_model"]:
                    retry = call["retry"]
                    retry.update(assessment="model_change_observed", previous_call_id=previous["id"],
                                 evidence=["same_thread", "request_model_changed"])
                    same_input = call["_input_fingerprint"] and call["_input_fingerprint"] == previous["_input_fingerprint"]
                    candidate = model in previous["safety"]["faster_models"]
                    same_purpose = call["purpose"] == previous["purpose"]
                    if same_input and candidate and same_purpose and not call["_previous_response_id"]:
                        retry["assessment"] = "possible_faster_model_retry"
                        retry["evidence"] += ["same_input", "model_is_previously_advertised_candidate"]
                previous_by_thread[thread] = call
        selected = [c for c in self.calls if not exclude_prewarm or c["purpose"] != "prewarm"]
        public = [{k: v for k, v in c.items() if not k.startswith("_")} for c in selected]
        return {
            "analysis_schema_version": 1, "generated_at": datetime.now(timezone.utc).isoformat(),
            "input_files": [str(p) for p in files], "summary": {
                "unique_capture_events": self.unique_records, "duplicate_capture_events": self.duplicate_records,
                "non_chat_capture_events": self.ignored_records, "event_counts": dict(self.events),
                "all_chat_requests": len(self.calls), "included_requests": len(selected),
                "prewarm_requests": sum(c["purpose"] == "prewarm" for c in self.calls),
                "model_comparison": dict(Counter(c["model_match"] for c in selected)),
                "waiting_state": dict(Counter(c["safety"]["waiting_state"] for c in selected)),
                "retry_assessment": dict(Counter(c["retry"]["assessment"] for c in selected)),
                "warnings": len(self.warnings),
            }, "calls": public, "warnings": self.warnings, "notes": NOTES,
        }


def analyze(paths: Iterable[Path], *, exclude_prewarm: bool = False) -> dict:
    files = discover_inputs(paths)
    analyzer = Analyzer()
    for path in files:
        for source, row in read_records(path, analyzer.warnings):
            analyzer.accept(source, row)
    return analyzer.report(files, exclude_prewarm)


PURPOSES = {"prewarm": "预热", "title_generation": "标题生成（推断）", "conversation": "聊天"}
MATCHES = {"match": "一致", "mismatch": "不一致", "unknown": "未知"}
RETRIES = {"not_observed": "未观察到", "model_change_observed": "同线程模型已变化（原因未知）",
           "possible_faster_model_retry": "疑似更快模型重试（未确认点击）"}
WAITING = {"observed": "已报告等待", "not_observed": "未观察到等待", "unknown": "未知"}


def cell(value: Any) -> str:
    return str(value if value is not None else "未知").replace("|", "\\|").replace("\n", " ").replace("\r", " ")


def render_markdown(report: dict) -> str:
    summary = report["summary"]
    counts = summary["model_comparison"]
    lines = ["# 聊天请求分析", "", f"分析 {summary['included_requests']} 次调用；模型一致 {counts.get('match', 0)} 次，"
             f"不一致 {counts.get('mismatch', 0)} 次，未知 {counts.get('unknown', 0)} 次。"
             f"去重 {summary['duplicate_capture_events']} 条事件。", "",
             "| 调用 | 用途 / 会话来源 | 请求模型 | 返回模型 | 比较 | 安全机制启用 | 等待状态 | 更快模型候选 | 重试判断 |",
             "| --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    for call in report["calls"]:
        safety = call["safety"]
        values = [call["id"], PURPOSES[call["purpose"]] + " / " + (call["thread_source"] or "未知"),
                  call["request_model"], ", ".join(call["response_models"]) or None,
                  MATCHES[call["model_match"]], {True: "是", False: "否", None: "未知"}[safety["enabled"]],
                  WAITING[safety["waiting_state"]], ", ".join(safety["faster_models"]) or "未提供",
                  RETRIES[call["retry"]["assessment"]]]
        lines.append("| " + " | ".join(map(cell, values)) + " |")
    for call in report["calls"]:
        safety = call["safety"]
        lines += ["", f"## {call['id']}", "",
                  f"- 请求：`{cell(source_label(call['request_source']))}`",
                  f"- 完成：`{cell(source_label(call['completion_source']))}`" if call["completion_source"] else "- 完成：没有捕获到完成记录。",
                  f"- flow_id：`{cell(call['flow_id'])}`；response_id：`{cell(call['response_id'])}`",
                  f"- 状态：`{call['status']}`；耗时：{cell(call['duration_ms'])} ms。",
                  f"- 推理强度：{cell(call['request_effort'])} → {cell(', '.join(call['response_efforts']) or None)}。",
                  f"- 等待提示（分析器说明）：{safety['waiting_hint']}",
                  f"- 等待标记：true={safety['true_events']}，false={safety['false_events']}；界面是否显示：未知。",
                  f"- 换模重试：{RETRIES[call['retry']['assessment']]}；关联前次调用：{call['retry']['previous_call_id'] or '无'}。"]
        for item in safety["configuration_evidence"]:
            values = {k: v for k, v in item.items() if k not in {"source", "scope"}}
            lines.append(f"- 安全配置证据：`{cell(source_label(item['source']))}`，{cell(json.dumps(values, ensure_ascii=False))}（{item['scope']}）。")
        if call["issues"]:
            lines.append("- 数据限制：" + cell(", ".join(call["issues"])) + "。")
    lines += ["", "## 解释范围", "", *["- " + note for note in report["notes"]]]
    if report["warnings"]:
        lines += ["", "## 解析与关联问题", ""]
        lines.extend(f"- `{cell(source_label(w['source']))}`：{w['code']}" for w in report["warnings"])
    return "\n".join(lines) + "\n"


def write_reports(report: dict, directory: Path) -> tuple[Path, Path]:
    directory = directory.expanduser().resolve()
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    targets = (directory / "chat-analysis.json", directory / "chat-analysis.md")
    if any(str(p) in report["input_files"] for p in targets):
        raise ValueError("报告输出不能覆盖输入抓包文件")
    contents = (json.dumps(report, ensure_ascii=False, indent=2) + "\n", render_markdown(report))
    for target, content in zip(targets, contents):
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=directory, delete=False) as handle:
            temp = Path(handle.name)
            handle.write(content)
        try:
            os.chmod(temp, 0o600)
            os.replace(temp, target)
        finally:
            temp.unlink(missing_ok=True)
    return targets
