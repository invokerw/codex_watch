import gzip
import hashlib
import json
import unittest
from urllib.parse import parse_qs, urlsplit

from codex_watch.core import BodyCapture, body_snapshot, host_allowed, redact_headers, redact_url


class CaptureCoreTests(unittest.TestCase):
    def test_credentials_in_headers_url_and_nested_json(self):
        headers = redact_headers([
            ("Authorization", "Bearer header-secret"), ("Cookie", "session=cookie-secret"),
            ("Set-Cookie", "session=response-secret"), ("X-Api-Key", "api-secret"),
            ("Content-Type", "application/json"), ("Location", "https://a.test/?token=url-secret"),
        ])
        self.assertNotIn("secret", json.dumps(headers))
        self.assertIn(["Content-Type", "application/json"], headers)
        url = redact_url("https://alice:pw@api.openai.com/v1?api_key=secret&model=test&model=two#secret")
        self.assertNotIn("alice", url)
        self.assertNotIn("secret", url)
        self.assertEqual(parse_qs(urlsplit(url).query), {"api_key": ["[REDACTED]"], "model": ["test", "two"]})
        body = json.dumps({"input": [{"accessToken": "secret", "text": "Bearer abcdef"}]}).encode()
        snapshot = body_snapshot(body, include_body=True, max_bytes=4096)
        self.assertNotIn("secret", json.dumps(snapshot))
        self.assertNotIn("abcdef", json.dumps(snapshot))

    def test_default_records_size_and_hash_only(self):
        body = b"private prompt"
        self.assertEqual(body_snapshot(body, include_body=False, max_bytes=4096), {
            "bytes": len(body), "sha256": hashlib.sha256(body).hexdigest(), "captured": False,
        })

    def test_host_filter_does_not_match_lookalikes(self):
        for host in ("api.openai.com", "sub.api.openai.com", "API.OPENAI.COM."):
            self.assertTrue(host_allowed(host, "api.openai.com"))
        for host in ("example.com", "evilapi.openai.com", "api.openai.com.evil.test"):
            self.assertFalse(host_allowed(host, "api.openai.com"))
        self.assertTrue(host_allowed("example.com", ""))

    def test_large_compressed_stream_passes_through_with_bounded_memory(self):
        raw = b"a" * 100_000
        compressed = gzip.compress(raw)
        capture = BodyCapture(include_body=True, max_bytes=31, content_encoding="gzip")
        for i in range(0, len(compressed), 3):
            chunk = compressed[i:i + 3]
            self.assertEqual(capture.feed(chunk), chunk)
        result = capture.snapshot()
        self.assertEqual(result["text"], "a" * 31)
        self.assertTrue(result["truncated"])
        self.assertLessEqual(len(capture.sample), 32)
        self.assertEqual(result["bytes"], len(compressed))
        self.assertEqual(result["sha256"], hashlib.sha256(compressed).hexdigest())

    def test_truncated_json_does_not_fall_back_to_unredacted_text(self):
        body = b'{"access_token":"very-secret-value","input":"hello"}'
        snapshot = body_snapshot(body, include_body=True, max_bytes=25)
        self.assertEqual(snapshot["omitted"], "truncated_json")
        self.assertNotIn("very-secret", json.dumps(snapshot))

    def test_split_sse_json_and_incomplete_tail(self):
        capture = BodyCapture(include_body=True, max_bytes=1000, content_type="text/event-stream")
        raw = b'data: {"delta":"hello","token":"secret"}\r\n\r\ndata: [DONE]\n\ndata: {"token":"unfinished'
        for byte in raw:
            capture.feed(bytes([byte]))
        snapshot = capture.snapshot()
        self.assertIn("hello", snapshot["text"])
        self.assertIn("[DONE]", snapshot["text"])
        self.assertTrue(snapshot["incomplete_event_omitted"])
        self.assertNotIn("secret", snapshot["text"])
        self.assertNotIn("unfinished", snapshot["text"])

    def test_form_binary_and_unsupported_encoding(self):
        form = body_snapshot(b'password=secret&input=hi', include_body=True, max_bytes=100,
                             content_type="application/x-www-form-urlencoded")
        self.assertEqual(form["form"], [["password", "[REDACTED]"], ["input", "hi"]])
        binary = body_snapshot(b'\x00\xffsecret', include_body=True, max_bytes=100)
        self.assertIn("omitted", binary)
        compressed = body_snapshot(b'opaque', include_body=True, max_bytes=100, content_encoding="br")
        self.assertEqual(compressed["omitted"], "unsupported_content_encoding")

    def test_gzip_json_and_invalid_compressed_data(self):
        raw = gzip.compress(b'{"input":"hello","token":"secret"}')
        result = body_snapshot(raw, include_body=True, max_bytes=100, content_encoding="gzip")
        self.assertEqual(result["json"], {"input": "hello", "token": "[REDACTED]"})
        result = body_snapshot(raw[:-4], include_body=True, max_bytes=100, content_encoding="gzip")
        self.assertEqual(result["omitted"], "incomplete_compressed_body")

    def test_utf8_limit_only_discards_incomplete_final_character(self):
        body = "你好".encode()
        result = body_snapshot(body, include_body=True, max_bytes=4)
        self.assertEqual(result["text"], "你")
        result = body_snapshot(b"a\xffbcdef", include_body=True, max_bytes=4)
        self.assertEqual(result["omitted"], "non_utf8_body")


if __name__ == "__main__":
    unittest.main()
