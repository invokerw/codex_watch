import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from codex_watch.core import SSEEventDecoder
from codex_watch.monitor import ChatIndex, Monitor, latest_user_text, render_detail, render_search
from test_support import cli_command
from test_capture_analysis import request, response, ws, metadata


def user_request(model="model-a", flow="f1", thread="t1", turn="turn-1", content="修复登录失败", **extra):
    return request(model, flow, thread, input=[{"role": "user", "content": content}],
                   client_metadata={"x-codex-turn-metadata": json.dumps({
                       "thread_id": thread, "turn_id": turn, "thread_source": "user", "request_kind": "turn"})}, **extra)


class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "traffic.jsonl"
        self.source.touch()
        self.db_path = self.root / "index.sqlite3"
        self.index = ChatIndex(self.db_path)
        self.addCleanup(lambda: self.index.close())
        self.monitor = Monitor(self.source, self.index)
        self.sequence = 0

    def append(self, *rows):
        with self.source.open("a") as handle:
            for row in rows:
                self.sequence += 1
                row["index"] = self.sequence
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")

    def test_mismatch_alert_before_completion_and_search_by_question(self):
        self.append(user_request())
        lines, _ = self.monitor.poll()
        self.assertIn("[发送]", "\n".join(lines))
        self.append(response(model="model-b", kind="response.created"))
        lines, _ = self.monitor.poll()
        self.assertIn("[模型不一致]", "\n".join(lines))
        hits = self.index.search("登录")
        self.assertEqual(len(hits), 1)
        detail = self.index.show(hits[0]["id"], raw=True)
        self.assertEqual(detail["requests"][0]["status"], "pending")
        self.assertEqual(detail["requests"][0]["request_model"], "model-a")
        self.assertEqual(detail["requests"][0]["response_models"], ["model-b"])
        self.assertEqual(len(detail["requests"][0]["raw_events"]), 2)

    def test_tool_loop_grouped_by_turn_and_answer_not_duplicated(self):
        self.append(user_request(),
                    response(kind="response.created"),
                    ws({"type": "response.output_text.delta", "item_id": "item1", "delta": "检查完成"}),
                    ws({"type": "response.output_text.done", "item_id": "item1", "text": "检查完成"}), response(),
                    user_request(flow="f2"), response(flow="f2", rid="r2", kind="response.created"),
                    ws({"type": "response.output_text.delta", "item_id": "item2", "delta": "已修复"}, flow="f2"),
                    response(flow="f2", rid="r2"))
        self.monitor.poll()
        hits = self.index.search("修复")
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["request_count"], 2)
        detail = self.index.show(hits[0]["id"])
        self.assertEqual(detail["user_text"], "修复登录失败")
        self.assertEqual(detail["answer_text"], "检查完成\n\n已修复")
        self.assertEqual(len(self.index.search("检查完成")), 1)

    def test_restart_resumes_inflight_call_without_missing_or_duplicate_events(self):
        self.append(user_request(), response(kind="response.created"))
        self.monitor.poll()
        cid = self.index.search()[0]["id"]
        self.index.close()
        self.index = ChatIndex(self.db_path)
        self.monitor = Monitor(self.source, self.index)
        self.assertEqual(self.monitor.poll()[1], 0)
        self.append(ws({"type": "response.output_text.delta", "item_id": "m", "delta": "答案"}), response())
        lines, _ = self.monitor.poll()
        self.assertNotIn("[发送]", "\n".join(lines))
        self.assertIn("[一致]", "\n".join(lines))
        self.assertEqual(self.index.show(cid)["answer_text"], "答案")
        self.assertEqual(self.index.search()[0]["request_count"], 1)

    def test_partial_utf8_line_waits_for_newline(self):
        raw = json.dumps(user_request(content="中文问题"), ensure_ascii=False).encode() + b"\n"
        split = raw.index("中文".encode()) + 1
        self.source.write_bytes(raw[:split])
        self.assertEqual(self.monitor.poll()[1], 0)
        with self.source.open("ab") as handle:
            handle.write(raw[split:])
        self.assertEqual(self.monitor.poll()[1], 1)
        self.assertEqual(self.index.search("中文问题")[0]["user_text"], "中文问题")

    def test_rotation_archives_raw_records_and_deduplicates_replay(self):
        self.append(user_request(), response())
        self.monitor.poll()
        cid = self.index.search()[0]["id"]
        original = self.source.read_bytes()
        self.source.rename(self.root / "old.jsonl")
        self.source.write_bytes(original)
        self.monitor.poll()
        self.assertEqual(len(self.index.show(cid, raw=True)["requests"][0]["raw_events"]), 2)
        (self.root / "old.jsonl").unlink()
        self.source.write_bytes(b"")
        self.monitor.poll()
        self.append(user_request(flow="f2", turn="t2", content="第二个问题"), response(flow="f2", rid="r2"))
        self.monitor.poll()
        self.assertEqual(len(self.index.search()), 2)
        self.assertEqual(self.index.show(cid, raw=True)["requests"][0]["raw_events"][0]["record"]["flow_id"], "f1")

    def test_hides_prewarm_and_title_auxiliary_calls(self):
        self.append(user_request(generate=False), response(),
                    user_request(flow="title", text={"format": {"schema": {"properties": {"title": {}}}}}),
                    response(flow="title", rid="title-r"),
                    user_request(flow="real"), response(flow="real", rid="real-r"))
        lines, _ = self.monitor.poll()
        self.assertEqual(sum("[发送]" in line for line in lines), 1)
        self.assertEqual(len(self.index.search()), 1)

    def test_candidate_header_does_not_trigger_model_change_alert(self):
        self.append(user_request(), metadata(faster="model-b"), response(safety_buffering=False))
        lines, _ = self.monitor.poll()
        self.assertNotIn("不一致", "\n".join(lines))
        self.assertNotIn("变化", "\n".join(lines))

    def test_request_model_change_is_distinct_from_response_mismatch(self):
        self.append(user_request(), response(), user_request(model="model-b", flow="f2", turn="t2"),
                    response(model="model-b", flow="f2", rid="r2"))
        lines, _ = self.monitor.poll()
        self.assertIn("[请求模型变化]", "\n".join(lines))
        self.assertNotIn("[模型不一致]", "\n".join(lines))

    def test_sse_updates_before_http_response_and_final_body_is_not_replayed(self):
        from test_capture_analysis import record
        self.append(record("request", method="POST", request={"body": {"json": {"model": "model-a", "input": "流式聊天"}}}),
                    record("sse_event", body={"json": {"type": "response.created", "response": {"id": "r", "model": "model-b"}}}))
        lines, _ = self.monitor.poll()
        self.assertIn("[模型不一致]", "\n".join(lines))
        self.append(record("sse_event", body={"json": {"type": "response.output_text.delta", "item_id": "i", "delta": "hello"}}),
                    record("sse_event", body={"json": {"type": "response.completed", "response": {"id": "r", "model": "model-b"}}}),
                    record("response", response={"status_code": 200, "headers": [["content-type", "text/event-stream"]],
                           "body": {"text": 'data: {"type":"response.output_text.delta","delta":"hello"}\n\n'}}))
        self.monitor.poll()
        hit = self.index.search()[0]
        self.assertEqual(self.index.show(hit["id"])["answer_text"], "hello")
        self.assertEqual(self.index.show(hit["id"])["requests"][0]["status"], "completed")

    def test_readers_can_query_while_monitor_writes_and_writer_is_exclusive(self):
        self.append(user_request(), response())
        self.monitor.poll()
        reader = ChatIndex(self.db_path, readonly=True)
        try:
            self.assertEqual(len(reader.search()), 1)
        finally:
            reader.close()
        with self.assertRaisesRegex(ValueError, "已有 watch"):
            ChatIndex(self.db_path)

    def test_cli_show_raw_and_no_terminal_control_sequences(self):
        self.append(user_request(content="hello\x1b[31m world"), response())
        self.monitor.poll()
        hit = self.index.search("world")[0]
        self.assertNotIn("\x1b", render_search([hit]))
        self.assertNotIn("\x1b", render_detail(self.index.show(hit["id"])))
        command = [*cli_command(), "show", hit["id"], "--raw", "--index", str(self.db_path)]
        result = subprocess.run(command, capture_output=True, text=True, check=True, timeout=5)
        self.assertEqual(json.loads(result.stdout)["requests"][0]["raw_events"][0]["record"]["flow_id"], "f1")

    def test_latest_user_message_skips_history_and_environment(self):
        payload = {"input": [{"role": "developer", "content": "system instructions"},
                             {"role": "user", "content": "旧问题"}, {"role": "assistant", "content": "旧答案"},
                             {"role": "user", "content": "<environment_context>metadata</environment_context>"},
                             {"role": "user", "content": [{"type": "input_text", "text": "新问题"}, {"type": "input_image"}]}]}
        self.assertEqual(latest_user_text(payload), "新问题\n[图片]")

    def test_sse_decoder_handles_chunk_boundaries_and_redacts_json(self):
        result = []
        decoder = SSEEventDecoder(result.append)
        wire = 'data: {"model":"model-a","token":"secret","text":"中文"}\r\n\r\ndata: [DONE]\n\n'.encode()
        for byte in wire:
            decoder.feed(bytes([byte]))
        self.assertEqual(result[0]["text"], "中文")
        self.assertEqual(result[0]["token"], "[REDACTED]")
        self.assertEqual(result[-1]["type"], "capture.stream_done")


if __name__ == "__main__":
    unittest.main()
