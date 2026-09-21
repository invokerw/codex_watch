import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from codex_watch.analysis import analyze, render_markdown, write_reports
from tests.test_support import cli_command

URL = "https://chatgpt.com/backend-api/codex/responses"


def record(event, flow="f1", **values):
    return {"schema_version": 2, "event": event, "flow_id": flow, "url": URL,
            "captured_at": "2026-09-20T07:12:00+00:00", **values}


def ws(payload, flow="f1", direction="server_to_client"):
    return record("websocket_message", flow, direction=direction, body={"captured": True, "json": payload})


def request(model="model-a", flow="f1", thread="thread-1", **extra):
    return ws({"type": "response.create", "model": model,
               "input": [{"role": "user", "content": "PROMPT_MUST_NOT_APPEAR"}],
               "client_metadata": {"x-codex-turn-metadata": json.dumps({
                   "thread_id": thread, "thread_source": "user", "request_kind": "turn"})},
               **extra}, flow, "client_to_server")


def response(model="model-a", flow="f1", rid="r1", kind="response.completed", **extra):
    return ws({"type": kind, "response": {"id": rid, "model": model}, **extra}, flow)


def metadata(flow="f1", enabled=True, faster="model-b"):
    return ws({"type": "codex.response.metadata", "headers": {
        "X-Codex-Safety-Buffering-Enabled": str(enabled).lower(),
        "x-codex-safety-buffering-faster-model": faster,
    }}, flow)


class AnalysisTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)

    def run_report(self, rows, **kwargs):
        path = self.directory / "capture.jsonl"
        path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
        return analyze([path], **kwargs)

    def test_multiple_calls_same_socket_and_separate_system_thread(self):
        report = self.run_report([
            request(generate=False), metadata(), response(safety_buffering=False),
            request(), metadata(), response(rid="r2", safety_buffering=False),
            request(model="model-b", flow="f2", thread="title-thread", text={"format": {
                "type": "json_schema", "schema": {"properties": {"title": {"type": "string"}}}}}),
            response(model="model-b", flow="f2", rid="r3"),
        ])
        self.assertEqual(report["summary"]["all_chat_requests"], 3)
        self.assertEqual(report["summary"]["model_comparison"], {"match": 3})
        self.assertEqual([c["purpose"] for c in report["calls"]], ["prewarm", "conversation", "title_generation"])
        self.assertEqual(report["summary"]["retry_assessment"], {"not_observed": 3})
        self.assertNotIn("PROMPT_MUST_NOT_APPEAR", json.dumps(report))
        self.assertNotIn("_input_fingerprint", json.dumps(report))

    def test_enabled_is_not_active_and_missing_is_unknown(self):
        report = self.run_report([request(), metadata(), response(safety_buffering=False),
                                  request(flow="f2", thread="t2"), response(flow="f2", rid="r2")])
        first, second = report["calls"]
        self.assertTrue(first["safety"]["enabled"])
        self.assertEqual(first["safety"]["waiting_state"], "not_observed")
        self.assertEqual(first["safety"]["faster_models"], ["model-b"])
        self.assertIsNone(first["safety"]["ui_display_confirmed"])
        self.assertIsNone(second["safety"]["enabled"])
        self.assertEqual(second["safety"]["waiting_state"], "unknown")

    def test_true_wait_state_does_not_confirm_ui_or_model_switch(self):
        report = self.run_report([request(), metadata(),
                                  response(kind="response.created", safety_buffering=True),
                                  response(safety_buffering=False)])
        call = report["calls"][0]
        self.assertEqual(call["safety"]["waiting_state"], "observed")
        self.assertEqual(call["safety"]["true_events"], 1)
        self.assertEqual(call["safety"]["false_events"], 1)
        self.assertEqual(call["retry"]["assessment"], "not_observed")

    def test_candidate_retry_and_unrelated_model_change(self):
        report = self.run_report([
            request(), metadata(), response(safety_buffering=True),
            request(model="model-b", flow="f2"), response(model="model-b", flow="f2", rid="r2"),
            request(model="model-c", flow="f3", input=[{"role": "user", "content": "a different question"}]),
            response(model="model-c", flow="f3", rid="r3"),
        ])
        self.assertEqual(report["calls"][1]["retry"]["assessment"], "possible_faster_model_retry")
        self.assertEqual(report["calls"][1]["retry"]["previous_call_id"], "call-0001")
        self.assertIsNone(report["calls"][1]["retry"]["ui_action_confirmed"])
        self.assertEqual(report["calls"][2]["retry"]["assessment"], "model_change_observed")

    def test_followup_response_id_does_not_claim_retry(self):
        report = self.run_report([request(), metadata(), response(),
                                  request(model="model-b", flow="f2", previous_response_id="r1"),
                                  response(model="model-b", flow="f2", rid="r2")])
        self.assertEqual(report["calls"][1]["retry"]["assessment"], "model_change_observed")

    def test_mismatch_in_any_response_stage_is_reported(self):
        report = self.run_report([request(), response(model="model-b", kind="response.created"), response()])
        call = report["calls"][0]
        self.assertEqual(call["model_match"], "mismatch")
        self.assertEqual(call["response_models"], ["model-b", "model-a"])
        self.assertEqual([o["source"]["line"] for o in call["model_observations"]], [2, 3])

    def test_unavailable_request_body_never_counts_as_a_match(self):
        report = self.run_report([
            record("websocket_message", direction="client_to_server",
                   body={"captured": False, "truncated": True, "omitted": "truncated_json"}),
            response(),
        ])
        call = report["calls"][0]
        self.assertEqual(call["model_match"], "unknown")
        self.assertIn("truncated_json", call["issues"])

    def test_http_json_and_multiline_sse(self):
        http_request = record("request", method="POST", request={"body": {"json": {"model": "model-a"}}})
        events = ('event: response.created\ndata: {"type":"response.created",\n'
                  'data: "response":{"id":"r1","model":"model-a"},"safety_buffering":true}\n\n'
                  'data: {"type":"response.completed","response":{"id":"r1","model":"model-b"}}')
        report = self.run_report([http_request, record("response", response={
            "status_code": 200, "headers": [["Content-Type", "text/event-stream"]], "body": {"text": events}})])
        self.assertEqual(report["calls"][0]["model_match"], "mismatch")
        self.assertEqual(report["calls"][0]["safety"]["waiting_state"], "observed")
        self.assertEqual(report["calls"][0]["status"], "completed")
        direct = self.run_report([http_request, record("response", response={
            "status_code": 200, "body": {"json": {"object": "response", "model": "model-a", "status": "completed"}}})])
        self.assertEqual(direct["calls"][0]["status"], "completed")

    def test_chat_completions_done_marker_and_partial_sse(self):
        req = record("request", method="POST", url="https://api.openai.com/v1/chat/completions",
                     request={"body": {"json": {"model": "model-a"}}})
        text = 'data: {"object":"chat.completion.chunk","id":"r","model":"model-a"}\n\ndata: [DONE]'
        res = record("response", url=req["url"], response={"status_code": 200,
                     "headers": [["content-type", "text/event-stream"]], "body": {"text": text}})
        self.assertEqual(self.run_report([req, res])["calls"][0]["status"], "completed")
        res["response"]["body"]["text"] = text.split("\n\ndata: [DONE]")[0]
        self.assertEqual(self.run_report([req, res])["calls"][0]["status"], "incomplete")

    def test_ambiguous_overlapping_requests_are_not_guessed(self):
        report = self.run_report([request(), request(model="model-b"), response()])
        self.assertEqual(report["summary"]["model_comparison"], {"unknown": 2})
        self.assertEqual(report["warnings"][0]["code"], "ambiguous_response_association")

    def test_response_ids_handle_interleaving_after_binding(self):
        report = self.run_report([request(), response(kind="response.created"),
                                  request(model="model-b"), response(model="model-b", rid="r2", kind="response.created"),
                                  response(), response(model="model-b", rid="r2")])
        self.assertEqual(report["summary"]["model_comparison"], {"match": 2})
        self.assertEqual(report["warnings"], [])

    def test_deduplicate_pretty_dump_and_jsonl_keep_source_line(self):
        rows = [request(), response()]
        path = self.directory / "capture.jsonl"
        path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
        (self.directory / "dump").write_text("\n".join(json.dumps(r, indent=2) for r in rows))
        report = analyze([self.directory])
        self.assertEqual(report["summary"]["included_requests"], 1)
        self.assertEqual(report["summary"]["duplicate_capture_events"], 2)
        self.assertEqual(report["calls"][0]["completion_source"]["line"], 2)
        self.assertEqual(report["calls"][0]["completion_source"]["file"], str(path.resolve()))
        pretty = analyze([self.directory / "dump"])
        expected_line = len(json.dumps(rows[0], indent=2).splitlines()) + 1
        self.assertEqual(pretty["calls"][0]["completion_source"]["line"], expected_line)

    def test_invalid_line_missing_completion_and_exclude_prewarm(self):
        self.run_report([request(generate=False), response(), request()])
        path = self.directory / "capture.jsonl"
        with path.open("a") as handle:
            handle.write('{"incomplete":')
        report = analyze([path], exclude_prewarm=True)
        self.assertEqual(report["summary"]["all_chat_requests"], 2)
        self.assertEqual(report["summary"]["included_requests"], 1)
        self.assertEqual(report["summary"]["prewarm_requests"], 1)
        self.assertEqual(report["calls"][0]["status"], "pending")
        self.assertEqual(report["warnings"][0]["code"], "invalid_json_line")

    def test_safety_detail_object_preserves_unknown_boolean(self):
        report = self.run_report([request(), response(safety_buffering={
            "use_cases": ["example"], "reasons": ["review"], "retry_model": "model-c"})])
        safety = report["calls"][0]["safety"]
        self.assertEqual(safety["waiting_state"], "unknown")
        self.assertEqual(safety["faster_models"], ["model-c"])
        self.assertEqual(safety["details"][0]["reasons"], ["review"])

    def test_reports_and_cli_are_offline_and_machine_readable(self):
        report = self.run_report([request(), metadata(), response(safety_buffering=False)])
        paths = write_reports(report, self.directory / "reports")
        self.assertEqual(json.loads(paths[0].read_text())["summary"]["included_requests"], 1)
        self.assertEqual(paths[0].stat().st_mode & 0o777, 0o600)
        self.assertIn("等待提示", paths[1].read_text())
        self.assertIn("未观察到", render_markdown(report))
        command = [*cli_command(), "analyze",
                   str(self.directory / "capture.jsonl"), "--format", "json", "--output-dir", str(self.directory / "cli reports")]
        completed = subprocess.run(command, capture_output=True, text=True, check=True, timeout=5)
        self.assertEqual(json.loads(completed.stdout)["summary"]["model_comparison"], {"match": 1})


if __name__ == "__main__":
    unittest.main()
