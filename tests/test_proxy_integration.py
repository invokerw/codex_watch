"""End-to-end tests using only loopback servers; no Codex account required."""
from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone
import gzip
import hashlib
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
import os
from pathlib import Path
import signal
import socket
import ssl
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
import codex_watch
from tests.test_support import cli_command

ROOT = Path(__file__).resolve().parents[1]


def read_exact(stream, count):
    data = bytearray()
    while len(data) < count:
        chunk = stream.read(count - len(data))
        if not chunk:
            raise EOFError("Unexpected end of WebSocket frame")
        data.extend(chunk)
    return bytes(data)


def read_frame(stream):
    first, second = read_exact(stream, 2)
    length = second & 127
    if length == 126:
        length = struct.unpack("!H", read_exact(stream, 2))[0]
    elif length == 127:
        length = struct.unpack("!Q", read_exact(stream, 8))[0]
    mask = read_exact(stream, 4) if second & 128 else None
    data = read_exact(stream, length)
    if mask:
        data = bytes(c ^ mask[i % 4] for i, c in enumerate(data))
    return first & 15, data


def frame(payload, opcode=1, masked=False):
    assert len(payload) < 126
    header = bytes([128 | opcode, len(payload) | (128 if masked else 0)])
    if not masked:
        return header + payload
    mask = b"test"
    return header + mask + bytes(c ^ mask[i % 4] for i, c in enumerate(payload))


def write_certificate(directory):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "local-capture-test")])
    now = datetime.now(timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(subject).issuer_name(subject)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=1)).not_valid_after(now + timedelta(days=1))
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
            .add_extension(x509.SubjectAlternativeName([
                x509.IPAddress(ipaddress.ip_address("127.0.0.1")), x509.DNSName("localhost")]), critical=False)
            .sign(key, hashes.SHA256()))
    cert_path, key_path = directory / "origin.pem", directory / "origin-key.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                          serialization.NoEncryption()))
    return cert_path, key_path


class Origin(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        self.server.received = (self.headers.get("Authorization"), body)
        if self.path == "/v1/responses":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(b'data: {"type":"response.created","response":{"id":"resp-live","object":"response","model":"model-b"}}\n\n')
            self.wfile.flush()
            if self.server.release_model_stream.wait(8):
                self.wfile.write(b'data: {"type":"response.completed","response":{"id":"resp-live","object":"response","model":"model-b","status":"completed"}}\n\n')
                self.wfile.flush()
            self.close_connection = True
            return
        payload = gzip.compress(b'{"answer":"hello","access_token":"response-secret"}')
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Encoding", "gzip")
        self.send_header("Set-Cookie", "sid=cookie-secret")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        if self.path == "/sse":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(b'data: {"delta":"first","token":"sse-secret"}\n\n')
            self.wfile.flush()
            # The client releases this only after it has received the first event.
            if self.server.release_stream.wait(5):
                self.wfile.write(b'data: [DONE]\n\n')
                self.wfile.flush()
            self.close_connection = True
        elif self.path == "/ws":
            accept = base64.b64encode(hashlib.sha1(
                (self.headers["Sec-WebSocket-Key"] + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()
            ).digest()).decode()
            self.send_response(101)
            self.send_header("Upgrade", "websocket")
            self.send_header("Connection", "Upgrade")
            self.send_header("Sec-WebSocket-Accept", accept)
            self.end_headers()
            self.wfile.flush()
            while True:
                opcode, data = read_frame(self.rfile)
                self.wfile.write(frame(data, opcode=opcode))
                self.wfile.flush()
                if opcode == 8:
                    break
            self.close_connection = True
        elif self.path == "/disconnect":
            self.close_connection = True
            self.connection.shutdown(socket.SHUT_RDWR)
        else:
            self.send_response(204)
            self.end_headers()


class ProxyIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix="codex-capture-test-")
        directory = Path(cls.temp.name)
        cls.output = directory / "capture.jsonl"
        cls.confdir = directory / "mitm"
        cert, key = write_certificate(directory)
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Origin)
        cls.server.daemon_threads = True
        cls.server.release_stream = threading.Event()
        cls.server.release_model_stream = threading.Event()
        tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        tls.load_cert_chain(cert, key)
        cls.server.socket = tls.wrap_socket(cls.server.socket, server_side=True)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            cls.proxy_port = sock.getsockname()[1]
        cls.log = (directory / "proxy.log").open("w+")
        addon = (Path(codex_watch.__file__).parent / "addon.py" if os.environ.get("CODEX_WATCH_TEST_INSTALLED") == "1"
                 else ROOT / "codex_capture.py")
        cls.process = subprocess.Popen([
            str(Path(sys.executable).parent / "mitmdump"), "-q", "-s", str(addon),
            "--listen-host", "127.0.0.1", "--listen-port", str(cls.proxy_port),
            "--set", f"confdir={cls.confdir}", "--set", "connection_strategy=lazy",
            "--set", f"capture_output={cls.output}", "--set", "capture_hosts=127.0.0.1",
            "--set", "capture_body=true", "--set", f"ssl_verify_upstream_trusted_ca={cert}",
        ], stdout=cls.log, stderr=subprocess.STDOUT, cwd=ROOT)
        cls.addClassCleanup(cls.cleanup)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if cls.process.poll() is not None:
                break
            try:
                with socket.create_connection(("127.0.0.1", cls.proxy_port), timeout=0.2):
                    if (cls.confdir / "mitmproxy-ca-cert.pem").exists() and cls.output.exists():
                        return
            except OSError:
                pass
            time.sleep(0.05)
        cls.log.seek(0)
        raise RuntimeError("Proxy failed to start:\n" + cls.log.read())

    @classmethod
    def cleanup(cls):
        cls.server.release_stream.set()
        cls.server.release_model_stream.set()
        cls.process.terminate()
        try:
            cls.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            cls.process.kill()
            cls.process.wait()
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)
        cls.log.close()
        cls.temp.cleanup()

    def connection(self, host="127.0.0.1", port=None):
        context = ssl.create_default_context(cafile=str(self.confdir / "mitmproxy-ca-cert.pem"))
        conn = http.client.HTTPSConnection("127.0.0.1", self.proxy_port, timeout=3, context=context)
        conn.set_tunnel(host, port or self.server.server_port)
        self.addCleanup(conn.close)
        return conn

    def records(self, predicate):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            rows = [json.loads(line) for line in self.output.read_text().splitlines()]
            selected = [row for row in rows if predicate(row)]
            if selected:
                return selected
            time.sleep(0.02)
        self.fail("Expected capture event was not written")

    def test_https_preserves_payload_and_redacts_capture(self):
        conn = self.connection()
        body = b'{"input":"hello","api_key":"request-secret"}'
        conn.request("POST", "/v1/responses?token=query-secret", body,
                     {"Authorization": "Bearer header-secret", "Content-Type": "application/json"})
        response = conn.getresponse()
        self.assertEqual(response.status, 200)
        self.assertIn(b"response-secret", gzip.decompress(response.read()))
        self.assertEqual(self.server.received, ("Bearer header-secret", body))
        records = self.records(lambda r: r["event"] == "response" and "/v1/responses" in r["url"])
        self.assertEqual(records[-1]["response"]["body"]["json"]["access_token"], "[REDACTED]")
        self.assertNotIn("secret", self.output.read_text())
        self.assertEqual(self.output.stat().st_mode & 0o777, 0o600)

    def test_sse_arrives_before_server_finishes(self):
        conn = self.connection()
        conn.request("GET", "/sse")
        response = conn.getresponse()
        self.assertIn(b"first", response.readline())
        self.assertFalse(self.server.release_stream.is_set())
        self.server.release_stream.set()
        self.assertIn(b"[DONE]", response.read())
        record = self.records(lambda r: r["event"] == "response" and r["url"].endswith("/sse"))[-1]
        self.assertIn("first", record["response"]["body"]["text"])
        self.assertNotIn("sse-secret", json.dumps(record))

    def test_sse_model_mismatch_indexed_before_completion(self):
        from codex_watch.monitor import ChatIndex, Monitor

        index = ChatIndex(Path(self.temp.name) / "live-index.sqlite3")
        self.addCleanup(index.close)
        monitor = Monitor(self.output, index)
        monitor.poll(emit=False)
        conn = self.connection()
        conn.request("POST", "/v1/responses", json.dumps({"model": "model-a", "input": "live model probe"}),
                     {"Content-Type": "application/json"})
        try:
            response = conn.getresponse()
            self.assertIn(b"model-b", response.readline())
            self.records(lambda r: r["event"] == "sse_event" and r["body"]["json"].get("type") == "response.created")
            self.assertFalse(self.server.release_model_stream.is_set())
            lines, _ = monitor.poll()
            self.assertTrue(any("[模型不一致]" in line for line in lines), lines)
            chat = index.show(index.search("live model probe")[0]["id"])
            self.assertEqual(chat["requests"][0]["status"], "pending")
            self.assertEqual(chat["requests"][0]["response_models"], ["model-b"])
        finally:
            self.server.release_model_stream.set()
        self.assertIn(b"response.completed", response.read())
        self.records(lambda r: r["event"] == "response" and r["url"].endswith("/v1/responses"))
        monitor.poll()
        self.assertEqual(index.show(chat["id"])["requests"][0]["status"], "completed")

    def test_watch_starts_and_stops_its_proxy(self):
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        directory = Path(self.temp.name) / "watch"
        directory.mkdir()
        log_path = directory / "watch.log"
        with log_path.open("w") as log:
            process = subprocess.Popen([
                *cli_command(), "watch", "--port", str(port),
                "--confdir", str(self.confdir), "--file", str(directory / "capture.jsonl"),
                "--index", str(directory / "index.sqlite3"),
            ], stdout=log, stderr=subprocess.STDOUT)
            try:
                deadline = time.monotonic() + 15
                while time.monotonic() < deadline:
                    content = log_path.read_text()
                    if "正在监听聊天" in content:
                        break
                    if process.poll() is not None:
                        self.fail(content)
                    time.sleep(0.05)
                else:
                    self.fail("Watch failed to become ready: " + log_path.read_text())
                with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                    pass
                process.send_signal(signal.SIGINT)
                self.assertEqual(process.wait(timeout=8), 0, log_path.read_text())
                with self.assertRaises(OSError):
                    socket.create_connection(("127.0.0.1", port), timeout=0.2)
                self.assertIn("聊天索引已保存", log_path.read_text())
            finally:
                if process.poll() is None:
                    process.send_signal(signal.SIGINT)
                    try:
                        process.wait(timeout=8)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()

    def test_websocket_bidirectional_capture(self):
        conn = self.connection()
        conn.connect()
        conn.sock.sendall((
            "GET /ws HTTP/1.1\r\nHost: 127.0.0.1\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
            "Sec-WebSocket-Version: 13\r\nSec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n\r\n"
        ).encode())
        stream = conn.sock.makefile("rb")
        self.addCleanup(stream.close)
        self.assertIn(b"101", stream.readline())
        while stream.readline() != b"\r\n":
            pass
        payload = b'{"type":"response.create","token":"ws-secret"}'
        conn.sock.sendall(frame(payload, masked=True))
        self.assertEqual(read_frame(stream), (1, payload))
        conn.sock.sendall(frame(struct.pack("!H", 1000), opcode=8, masked=True))
        self.assertEqual(read_frame(stream)[0], 8)
        self.records(lambda r: r["event"] == "websocket_end")
        rows = self.records(lambda r: r["event"] == "websocket_message")
        self.assertEqual({r["direction"] for r in rows}, {"client_to_server", "server_to_client"})
        self.assertTrue(all(r["body"]["json"]["token"] == "[REDACTED]" for r in rows))

    def test_excluded_hostname_produces_no_capture(self):
        conn = self.connection(host="localhost")
        conn.request("GET", "/excluded")
        self.assertEqual(conn.getresponse().status, 204)
        conn.close()
        # A subsequent captured request acts as a barrier on the event loop.
        included = self.connection()
        included.request("GET", "/barrier")
        included.getresponse().read()
        self.records(lambda r: r["event"] == "response" and r["url"].endswith("/barrier"))
        self.assertNotIn("excluded", self.output.read_text())

    def test_upstream_disconnect_logs_error(self):
        conn = self.connection()
        conn.request("GET", "/disconnect")
        try:
            response = conn.getresponse()
            response.read()
        except (http.client.HTTPException, OSError):
            pass
        record = self.records(lambda r: r["event"] == "error" and r["url"].endswith("/disconnect"))[-1]
        self.assertIn("error", record)

    def test_launcher_and_codex_wrapper(self):
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        output = Path(self.temp.name) / "launcher.jsonl"
        with (Path(self.temp.name) / "launcher.log").open("w+") as log:
            process = subprocess.Popen([
                *cli_command(), "serve", "--port", str(port),
                "--confdir", str(self.confdir), "--output", str(output),
            ], stdout=log, stderr=subprocess.STDOUT)
            try:
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    if process.poll() is not None:
                        log.seek(0)
                        self.fail(log.read())
                    try:
                        with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                            break
                    except OSError:
                        time.sleep(0.05)
                else:
                    self.fail("Launcher did not open its proxy port")
                # A fake codex executable inspects only the wrapper's public settings.
                # It performs no model requests and doesn't read any user credentials.
                import os
                fake_bin = Path(self.temp.name) / "bin"
                fake_bin.mkdir(exist_ok=True)
                fake = fake_bin / "codex"
                fake.write_text(
                    f"#!{sys.executable}\nimport json, os, sys\n"
                    "print(json.dumps({'args': sys.argv[1:], 'proxy': os.environ['HTTPS_PROXY'], "
                    "'ca': os.environ['CODEX_CA_CERTIFICATE'], 'no_proxy': os.environ['NO_PROXY']}))\n"
                )
                fake.chmod(0o700)
                env = dict(os.environ, PATH=str(fake_bin) + os.pathsep + os.environ.get("PATH", ""),
                           NO_PROXY="*")
                wrapped = subprocess.run([
                    *cli_command(), "run", "--port", str(port),
                    "--confdir", str(self.confdir), "--", "exec", "prompt with spaces",
                ], env=env, capture_output=True, text=True, timeout=5, check=True)
                details = json.loads(wrapped.stdout)
                self.assertEqual(details["args"], ["exec", "prompt with spaces"])
                self.assertEqual(details["proxy"], f"http://127.0.0.1:{port}")
                self.assertEqual(Path(details["ca"]), (self.confdir / "mitmproxy-ca-cert.pem").resolve())
                self.assertNotEqual(details["no_proxy"], "*")
                self.assertTrue(output.exists())
            finally:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()


if __name__ == "__main__":
    unittest.main()
