"""Incremental chat monitor with a durable, searchable SQLite index."""
from __future__ import annotations

from collections import OrderedDict
import errno
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import zlib

from codex_watch.analysis import Analyzer, chat_endpoint, digest, stamp
from codex_watch.core import redact_text
from codex_watch.platform_support import child_options, display_command, lock_file, restrict_file, stop_child

FINISHED = {"completed", "failed", "incomplete", "cancelled", "http_finished", "connection_closed_without_completion"}


def pack(value):
    return zlib.compress(json.dumps(value, ensure_ascii=False).encode(), level=3)


def unpack(value):
    return json.loads(zlib.decompress(value))


def safe_terminal(value):
    value = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", str(value))
    return "".join(c for c in value if c in "\n\t" or ord(c) >= 32 and not 127 <= ord(c) < 160)


def preview(text, limit=90):
    text = " ".join(safe_terminal(text).split())
    return text[:limit] + ("…" if len(text) > limit else "")


def latest_user_text(payload):
    items = payload.get("input", payload.get("messages", []))
    if isinstance(items, str):
        return redact_text(items)
    if not isinstance(items, list):
        return ""
    for item in reversed(items):
        if not isinstance(item, dict) or item.get("role") != "user":
            continue
        content = item.get("content", [])
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            parts = []
            for part in content:
                if not isinstance(part, dict):
                    continue
                if isinstance(part.get("text"), str):
                    parts.append(part["text"])
                elif part.get("type") in {"input_image", "image_url"}:
                    parts.append("[图片]")
                elif part.get("type") in {"input_audio", "input_file", "file"}:
                    parts.append("[附件]")
            text = "\n".join(parts)
        else:
            continue
        if re.fullmatch(r"\s*<environment_context>.*</environment_context>\s*", text, re.S):
            continue
        if text.strip():
            return redact_text(text)
    return ""


def collect_answer(call, payload):
    parts = call.setdefault("_answer_parts", {})
    kind = payload.get("type", "")
    key = str(payload.get("item_id", payload.get("output_index", 0))) + ":" + str(payload.get("content_index", 0))
    if kind == "response.output_text.delta" and isinstance(payload.get("delta"), str):
        parts[key] = parts.get(key, "") + payload["delta"]
    elif kind == "response.output_text.done" and isinstance(payload.get("text"), str):
        parts[key] = payload["text"]
    elif kind in {"response.output_item.done", "response.output_item.added"}:
        item = payload.get("item") or {}
        if item.get("type") == "message":
            for i, part in enumerate(item.get("content", [])):
                if isinstance(part, dict) and isinstance(part.get("text"), str) and part["text"]:
                    parts[str(item.get("id", payload.get("output_index", 0))) + ":" + str(i)] = part["text"]
    response = payload.get("response") if isinstance(payload.get("response"), dict) else payload
    if response.get("object") == "response":
        for i, item in enumerate(response.get("output", [])):
            if item.get("type") == "message":
                for j, part in enumerate(item.get("content", [])):
                    if isinstance(part, dict) and isinstance(part.get("text"), str) and part["text"]:
                        parts[str(item.get("id", i)) + ":" + str(j)] = part["text"]
    for choice in response.get("choices", []):
        index = "choice:" + str(choice.get("index", 0))
        message = choice.get("message") or {}
        delta = choice.get("delta") or {}
        if isinstance(message.get("content"), str):
            parts[index] = message["content"]
        elif isinstance(delta.get("content"), str):
            parts[index] = parts.get(index, "") + delta["content"]
    # The captured body already has a limit; also bound incremental answer storage.
    remaining = 1048576
    for name in list(parts):
        parts[name] = redact_text(parts[name][:remaining])
        remaining -= len(parts[name])
    call["_answer_text"] = "\n".join(value for value in parts.values() if value)


def comparison(call):
    models = call["response_models"]
    if not call["request_model"] or not models:
        return "unknown"
    return "match" if all(m == call["request_model"] for m in models) else "mismatch"


class ChatIndex:
    def __init__(self, path: Path, *, readonly=False):
        self.path = path.expanduser().resolve()
        self.lock = None
        if readonly:
            if not self.path.exists():
                raise ValueError("尚无聊天索引。先运行 watch，或运行 watch --once 导入已有抓包。")
            self.db = sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True, timeout=10)
        else:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            self.lock = os.open(str(self.path) + ".lock", os.O_CREAT | os.O_RDWR, 0o600)
            try:
                lock_file(self.lock)
            except OSError as exc:
                os.close(self.lock)
                self.lock = None
                if exc.errno in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                    raise ValueError("已有 watch 正在更新此索引；可以直接使用 search / show 查询。") from exc
                raise
            fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
            restrict_file(fd)
            os.close(fd)
            self.db = sqlite3.connect(self.path, timeout=10)
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.executescript('''
                CREATE TABLE IF NOT EXISTS chats (
                    id TEXT PRIMARY KEY, user_text TEXT NOT NULL, answer_text TEXT NOT NULL,
                    search_text TEXT NOT NULL, updated_at TEXT, mismatch INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS requests (
                    id TEXT PRIMARY KEY, chat_id TEXT, thread_id TEXT, status TEXT,
                    requested_at TEXT, state BLOB NOT NULL
                );
                CREATE INDEX IF NOT EXISTS requests_chat ON requests(chat_id);
                CREATE TABLE IF NOT EXISTS events (
                    request_id TEXT NOT NULL, fingerprint TEXT NOT NULL, source TEXT NOT NULL, record BLOB NOT NULL,
                    PRIMARY KEY(request_id, fingerprint)
                );
                CREATE TABLE IF NOT EXISTS processed (fingerprint TEXT PRIMARY KEY);
                CREATE TABLE IF NOT EXISTS checkpoints (path TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            ''')
            self.db.commit()
        self.db.row_factory = sqlite3.Row

    def close(self):
        self.db.close()
        if self.lock is not None:
            os.close(self.lock)
            self.lock = None

    def checkpoint(self, path):
        row = self.db.execute("SELECT value FROM checkpoints WHERE path=?", (str(path),)).fetchone()
        return json.loads(row[0]) if row else None

    def save_checkpoint(self, path, value):
        self.db.execute("INSERT OR REPLACE INTO checkpoints VALUES (?,?)", (str(path), json.dumps(value)))

    def load_recent(self):
        rows = self.db.execute("SELECT state FROM requests WHERE status='pending' OR id IN "
                               "(SELECT id FROM requests ORDER BY rowid DESC LIMIT 128)").fetchall()
        return [unpack(row[0]) for row in rows]

    def save(self, call, row, source):
        cid = call["_chat_id"]
        self.db.execute("INSERT INTO requests VALUES (?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
                        "status=excluded.status, state=excluded.state",
                        (call["id"], cid, call["thread_id"], call["status"], call["requested_at"], pack(call)))
        self.db.execute("INSERT OR IGNORE INTO events VALUES (?,?,?,?)",
                        (call["id"], digest(row), json.dumps(source), pack(row)))
        requests = self.request_states(cid)
        questions = list(dict.fromkeys(c.get("_user_text", "") for c in requests if c.get("_user_text")))
        answers = [c.get("_answer_text", "") for c in requests if c.get("_answer_text")]
        question, answer = "\n\n".join(questions), "\n\n".join(answers)
        self.db.execute("INSERT OR REPLACE INTO chats VALUES (?,?,?,?,?,?)",
                        (cid, question, answer, (question + "\n" + answer).casefold(),
                         row.get("captured_at"), int(any(comparison(c) == "mismatch" for c in requests))))

    def request_states(self, cid):
        return [unpack(row[0]) for row in self.db.execute(
            "SELECT state FROM requests WHERE chat_id=? ORDER BY rowid", (cid,))]

    def search(self, query="", limit=20):
        result = []
        for row in self.db.execute("SELECT * FROM chats WHERE instr(search_text,?) > 0 "
                                   "ORDER BY updated_at DESC, rowid DESC LIMIT ?", (query.casefold(), limit)):
            states = self.request_states(row["id"])
            result.append({**dict(row), "request_count": len(states),
                           "pending": any(c["status"] == "pending" for c in states),
                           "unknown": any(comparison(c) == "unknown" for c in states),
                           "request_models": list(dict.fromkeys(c["request_model"] for c in states)),
                           "response_models": list(dict.fromkeys(m for c in states for m in c["response_models"]))})
        return result

    def show(self, prefix, *, raw=False):
        rows = self.db.execute("SELECT * FROM chats WHERE substr(id,1,?)=?", (len(prefix), prefix)).fetchall()
        if len(rows) != 1:
            raise ValueError("没有找到该聊天编号。" if not rows else "编号前缀匹配多条聊天，请输入更完整的编号。")
        result = dict(rows[0])
        result.pop("search_text", None)
        result["requests"] = []
        for call in self.request_states(result["id"]):
            sources = self.db.execute("SELECT source,record FROM events WHERE request_id=? ORDER BY rowid", (call["id"],)).fetchall()
            request = {"id": call["id"], "flow_id": call["flow_id"], "thread_id": call["thread_id"],
                       "turn_id": call["turn_id"], "response_id": call["response_id"],
                       "request_model": call["request_model"], "response_models": call["response_models"],
                       "model_match": comparison(call), "status": call["status"],
                       "requested_at": call["requested_at"], "completed_at": call["completed_at"],
                       "request_source": call["request_source"], "completion_source": call["completion_source"],
                       "model_observations": call["model_observations"], "issues": call["issues"],
                       "request_model_change": call.get("_request_model_change"),
                       "safety": call["safety"], "user_text": call.get("_user_text", ""),
                       "answer_text": call.get("_answer_text", ""),
                       "event_sources": [json.loads(e["source"]) for e in sources]}
            if raw:
                request["raw_events"] = [{"source": json.loads(e["source"]), "record": unpack(e["record"])} for e in sources]
            result["requests"].append(request)
        return result


class LiveAnalyzer(Analyzer):
    def __init__(self, index):
        super().__init__()
        self.index = index
        self.dirty = OrderedDict()
        for call in index.load_recent():
            self.calls.append(call)
            if call["status"] == "pending":
                self.active[call["flow_id"]].append(call)
            if call["response_id"]:
                self.by_response[call["flow_id"], call["response_id"]] = call
            if call["transport"] == "http":
                self.http_calls[call["flow_id"]] = call
        saved = index.db.execute("SELECT value FROM settings WHERE key='connection_headers'").fetchone()
        if saved:
            self.connection_headers = json.loads(saved[0])

    def mark(self, call):
        if call.get("_visible"):
            self.dirty[call["id"]] = call

    def new_call(self, row, source, payload, issues, transport):
        call = super().new_call(row, source, payload, issues, transport)
        discriminator = row.get("index") if transport == "websocket" else "http"
        if discriminator is None:
            discriminator = [row.get("captured_at"), source.get("offset", source.get("line"))]
        call["id"] = digest([row["flow_id"], transport, discriminator])[:20]
        call["_visible"] = call["purpose"] == "conversation" and call["thread_source"] in {None, "user"}
        call["_user_text"] = latest_user_text(payload)
        group = ["turn", call["thread_id"], call["turn_id"]] if call["thread_id"] and call["turn_id"] else ["request", call["id"]]
        call["_chat_id"] = digest(group)[:12]
        call["_answer_parts"] = {}
        call["_answer_text"] = ""
        if call["_visible"] and call["thread_id"]:
            previous = self.index.db.execute("SELECT state FROM requests WHERE thread_id=? ORDER BY rowid DESC LIMIT 1",
                                             (call["thread_id"],)).fetchone()
            if previous:
                old = unpack(previous[0])
                if old["request_model"] and call["request_model"] and old["request_model"] != call["request_model"]:
                    call["_request_model_change"] = {"from": old["request_model"], "to": call["request_model"],
                                                       "previous_chat_id": old["_chat_id"], "cause": "unknown"}
        self.mark(call)
        return call

    def observe(self, call, payload, source, time):
        super().observe(call, payload, source, time)
        collect_answer(call, payload)
        self.mark(call)

    def finish(self, call, source, time, status):
        super().finish(call, source, time, status)
        self.mark(call)

    def process(self, row, source):
        super().process(row, source)
        if row["event"] in {"response", "error"} and row["flow_id"] in self.http_calls:
            self.mark(self.http_calls[row["flow_id"]])

    def trim(self):
        keep = [c for c in self.calls if c["status"] == "pending"]
        keep += [c for c in self.calls[-128:] if c not in keep]
        ids = {id(c) for c in keep}
        self.calls = keep
        self.by_response = {k: c for k, c in self.by_response.items() if id(c) in ids}
        self.http_calls = {k: c for k, c in self.http_calls.items() if id(c) in ids}
        self.active = type(self.active)(list, {k: v for k, v in self.active.items() if v})
        if len(self.connection_headers) > 256:
            self.connection_headers = dict(list(self.connection_headers.items())[-256:])
        self.seen.clear()
        self.warnings.clear()


def notification(call):
    cid = call["_chat_id"]
    old = call.get("_last_notification")
    state = [comparison(call), call["status"], call["response_models"][:]]
    call["_last_notification"] = state
    output = []
    if old is None:
        output.append(f"[发送] #{cid}  {preview(call['_user_text']) or '（未捕获用户文字）'}  请求={call['request_model'] or '未知'}")
        change = call.get("_request_model_change")
        if change:
            output.append(f"[请求模型变化] #{cid}  {change['from']} → {change['to']}（原因未知）")
    if state[0] == "mismatch" and (old is None or old[0] != "mismatch"):
        output.append(f"[模型不一致] #{cid}  请求 {call['request_model']} → 返回 {', '.join(call['response_models'])}")
    elif call["status"] in FINISHED and (old is None or old[1] not in FINISHED):
        label = "一致" if state[0] == "match" else "未知" if state[0] == "unknown" else "模型不一致"
        output.append(f"[{label}] #{cid}  {call['request_model'] or '未知'} → {', '.join(call['response_models']) or '未知'}  [{call['status']}]")
    return [safe_terminal(line) for line in output]


class Monitor:
    def __init__(self, source: Path, index: ChatIndex):
        self.source = source.expanduser().resolve()
        self.index = index
        self.analyzer = LiveAnalyzer(index)

    def poll(self, *, emit=True, limit=1000):
        if not self.source.exists():
            return [], 0
        checkpoint = self.index.checkpoint(self.source) or {}
        output = []
        count = 0
        with self.source.open("rb") as handle, self.index.db:
            stat = os.fstat(handle.fileno())
            offset, line = checkpoint.get("offset", 0), checkpoint.get("line", 0)
            identity = [stat.st_dev, stat.st_ino]
            same_file = checkpoint.get("identity") == identity and stat.st_size >= offset
            if same_file and offset:
                handle.seek(max(0, offset - 128))
                anchor = hashlib.sha256(handle.read(min(offset, 128))).hexdigest()
                same_file = anchor == checkpoint.get("anchor")
            if not same_file:
                offset, line = 0, 0
            handle.seek(offset)
            while count < limit:
                start = handle.tell()
                raw = handle.readline()
                if not raw or not raw.endswith(b"\n"):
                    handle.seek(start)
                    break
                line += 1
                count += 1
                source = {"file": str(self.source), "line": line, "offset": start, "bytes": len(raw)}
                try:
                    row = json.loads(raw)
                except (ValueError, UnicodeError):
                    output.append(f"[日志不完整] {self.source}:{line}：无法解析，已跳过。")
                    continue
                if not isinstance(row, dict) or not row.get("flow_id") or not chat_endpoint(row.get("url", "")):
                    continue
                fingerprint = digest(row)
                if self.index.db.execute("SELECT 1 FROM processed WHERE fingerprint=?", (fingerprint,)).fetchone():
                    continue
                self.analyzer.dirty.clear()
                before = len(self.analyzer.warnings)
                self.analyzer.accept(source, row)
                for call in self.analyzer.dirty.values():
                    output.extend(notification(call))
                    self.index.save(call, row, source)
                for warning in self.analyzer.warnings[before:]:
                    output.append(f"[无法关联] {self.source}:{line}：{warning['code']}")
                self.index.db.execute("INSERT INTO processed VALUES (?)", (fingerprint,))
            offset = handle.tell()
            handle.seek(max(0, offset - 128))
            anchor = hashlib.sha256(handle.read(min(offset, 128))).hexdigest()
            self.index.save_checkpoint(self.source, {"identity": identity, "offset": offset, "line": line, "anchor": anchor})
            self.index.db.execute("INSERT OR REPLACE INTO settings VALUES ('connection_headers',?)",
                                  (json.dumps(self.analyzer.connection_headers),))
        self.analyzer.trim()
        return output if emit else [], count


def render_search(rows):
    if not rows:
        return "没有匹配的聊天。"
    lines = []
    for row in rows:
        label = "模型不一致" if row["mismatch"] else "进行中" if row["pending"] else "未知" if row["unknown"] else "一致"
        models = ", ".join(m or "未知" for m in row["request_models"])
        returned = ", ".join(row["response_models"]) or "未知"
        lines.append(f"#{row['id']}  [{label}]  {models} → {returned}  ({row['request_count']} 次请求)\n  {preview(row['user_text']) or '（未捕获用户文字）'}")
    return safe_terminal("\n".join(lines))


def render_detail(chat):
    lines = [f"聊天 #{chat['id']}", "", "你的内容：", chat["user_text"] or "（未捕获）", "",
             "回复：", chat["answer_text"] or "（未捕获或尚未完成）", "", "对应请求："]
    for i, req in enumerate(chat["requests"], 1):
        label = {"match": "一致", "mismatch": "模型不一致", "unknown": "未知"}[req["model_match"]]
        lines += [f"{i}. [{label}] {req['request_model'] or '未知'} → {', '.join(req['response_models']) or '未知'} ({req['status']})",
                  f"   flow_id={req['flow_id']}  response_id={req['response_id'] or '未知'}"]
        for title, source in [("请求", req["request_source"]), ("完成", req["completion_source"])]:
            if source:
                lines.append(f"   {title}：{source['file']}:{source.get('line', '?')}")
        if req["request_model_change"]:
            change = req["request_model_change"]
            lines.append(f"   同线程请求模型变化：{change['from']} → {change['to']}（原因未知）")
        if req["issues"]:
            lines.append("   数据限制：" + ", ".join(req["issues"]))
    lines += ["", f"查看保存的原始事件：codex-watch show {chat['id']} --raw"]
    return safe_terminal("\n".join(lines))


def run_watch(args, cli_prefix):
    import socket
    import subprocess
    import sys
    import time

    index = ChatIndex(args.index)
    child = None
    proxy_log = None
    try:
        if args.once and not args.file.exists():
            raise ValueError(f"抓包文件不存在：{args.file}")
        if not args.attach and not args.once:
            try:
                with socket.create_connection(("127.0.0.1", args.port), timeout=0.3):
                    raise ValueError(f"端口 {args.port} 已有服务。已有抓包代理请用 watch --attach；否则先停止占用端口的程序。")
            except OSError:
                pass
            log_path = index.path.parent / "proxy.log"
            fd = os.open(log_path, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
            restrict_file(fd)
            proxy_log = os.fdopen(fd, "a")
            command = [*cli_prefix, "serve", "--body",
                       "--port", str(args.port), "--output", str(args.file.expanduser().resolve()),
                       "--confdir", str(args.confdir.expanduser().resolve()), "--hosts", args.hosts,
                       "--max-body-bytes", str(args.max_body_bytes)]
            if args.upstream:
                command += ["--upstream", args.upstream]
            child = subprocess.Popen(command, stdout=proxy_log, stderr=subprocess.STDOUT, **child_options())
            deadline = time.monotonic() + 12
            while time.monotonic() < deadline:
                if child.poll() is not None:
                    raise ValueError(f"抓包代理启动失败，检查端口/依赖。详情：{log_path}")
                try:
                    with socket.create_connection(("127.0.0.1", args.port), timeout=0.2):
                        ca = args.confdir.expanduser() / "mitmproxy-ca-cert.pem"
                        if ca.exists():
                            break
                except OSError:
                    pass
                time.sleep(0.05)
            else:
                raise ValueError(f"代理未在预期时间内就绪。详情：{log_path}")
        monitor = Monitor(args.file, index)
        while True:
            _, count = monitor.poll(emit=False)
            if count < 1000:
                break
        total = index.db.execute("SELECT count(*) FROM chats").fetchone()[0]
        if args.once:
            print(render_search(index.search()))
            return 0
        print(f"正在监听聊天 · 已索引 {total} 条 · Ctrl+C 停止", flush=True)
        print("只显示用户聊天；模型不一致会立即提示。search 搜索内容，show 查看原始数据。", flush=True)
        if not args.attach:
            command = [*cli_prefix, "run", "--port", str(args.port),
                       "--confdir", str(args.confdir.expanduser().resolve())]
            print("在另一个终端的项目目录启动 Codex：\n" + display_command(command), flush=True)
        elif not args.file.exists():
            print(f"等待抓包文件创建：{args.file}", flush=True)
        while True:
            if child is not None and child.poll() is not None:
                raise ValueError(f"抓包代理已退出。详情：{index.path.parent / 'proxy.log'}")
            lines, count = monitor.poll()
            for line in lines:
                print(line, flush=True)
            if count < 1000:
                time.sleep(0.2)
    except KeyboardInterrupt:
        print("\n监视已停止，聊天索引已保存。", flush=True)
        return 0
    finally:
        if child is not None:
            stop_child(child)
        if proxy_log is not None:
            proxy_log.close()
        index.close()
