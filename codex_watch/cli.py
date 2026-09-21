#!/usr/bin/env python3
"""Convenient local proxy launcher and Codex environment wrapper."""
from __future__ import annotations

import argparse
import importlib.metadata
import os
from pathlib import Path
import shutil
import socket
import sys

from codex_watch import __version__
from codex_watch.platform_support import data_directory, run_client
from codex_watch.lifecycle import data_locks


def port_number(value: str) -> int:
    port = int(value)
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("端口必须在 1 到 65535 之间")
    return port


def non_negative(value: str) -> int:
    number = int(value)
    if number < 0:
        raise argparse.ArgumentTypeError("大小不能为负数")
    return number


def client_environment(port: int, ca: Path) -> dict[str, str]:
    env = os.environ.copy()
    proxy = f"http://127.0.0.1:{port}"
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
        env[name] = env[name.lower()] = proxy
    # Prevent an inherited NO_PROXY=* from silently bypassing capture.
    env["NO_PROXY"] = env["no_proxy"] = "localhost,127.0.0.1,::1"
    env["CODEX_CA_CERTIFICATE"] = str(ca)
    return env


def main(argv: list[str] | None = None, *, legacy_root=None, cli_prefix=None) -> int:
    parser = argparse.ArgumentParser(description="Codex 本地请求抓包工具（HTTP / SSE / WebSocket）")
    parser.add_argument("--version", action="version", version=f"codex-watch {__version__}")
    parser.add_argument("--data-dir", type=Path, help="数据目录，也可设置 CODEX_WATCH_HOME")
    sub = parser.add_subparsers(dest="command", required=True)
    serve = sub.add_parser("serve", help="启动仅监听 127.0.0.1 的抓包代理")
    serve.add_argument("--port", type=port_number, default=8080)
    serve.add_argument("--body", action="store_true", help="记录脱敏后的正文，默认仅元数据")
    serve.add_argument("--max-body-bytes", type=non_negative, default=1048576)
    serve.add_argument("--hosts", default="api.openai.com,chatgpt.com", help="域名列表，空字符串匹配所有域名")
    serve.add_argument("--output", type=Path)
    serve.add_argument("--confdir", type=Path)
    serve.add_argument("--upstream", help="可选的上游 HTTP 代理，例如 http://127.0.0.1:7890")
    run = sub.add_parser("run", help="为新的 Codex CLI 进程配置代理和 CA")
    run.add_argument("--port", type=port_number, default=8080)
    run.add_argument("--confdir", type=Path)
    run.add_argument("codex_args", nargs=argparse.REMAINDER, help="-- 后的所有参数传给 codex")
    analyze_parser = sub.add_parser("analyze", help="离线分析聊天模型、安全等待与更快模型重试")
    analyze_parser.add_argument("inputs", nargs="*", type=Path, help="JSONL / JSON 文件或目录，默认 captures")
    analyze_parser.add_argument("--output-dir", type=Path,
                                help="JSON 和 Markdown 报告目录")
    analyze_parser.add_argument("--format", choices=("text", "json", "markdown"), default="text",
                                help="终端输出格式；报告始终保存 JSON 和 Markdown")
    analyze_parser.add_argument("--exclude-prewarm", action="store_true", help="报告中隐藏预热调用")
    watch = sub.add_parser("watch", help="启动抓包并实时监视聊天模型（推荐）")
    watch.add_argument("--file", type=Path)
    watch.add_argument("--index", type=Path)
    watch.add_argument("--attach", action="store_true", help="只监听已有抓包文件，不启动代理")
    watch.add_argument("--once", action="store_true", help="导入已有 JSONL 后退出，不启动代理")
    watch.add_argument("--port", type=port_number, default=8080)
    watch.add_argument("--confdir", type=Path)
    watch.add_argument("--hosts", default="api.openai.com,chatgpt.com")
    watch.add_argument("--max-body-bytes", type=non_negative, default=4194304)
    watch.add_argument("--upstream", help="已有上游 HTTP 代理地址")
    search = sub.add_parser("search", help="按聊天内容搜索；不填关键词则列出最近聊天")
    search.add_argument("query", nargs="?", default="")
    search.add_argument("--limit", type=int, default=20)
    search.add_argument("--index", type=Path)
    show = sub.add_parser("show", help="按聊天编号查看内容和对应请求")
    show.add_argument("chat_id")
    show.add_argument("--raw", action="store_true", help="输出 JSON，包含保存的原始抓包事件")
    show.add_argument("--json", action="store_true", help="输出结构化 JSON")
    show.add_argument("--index", type=Path)
    doctor = sub.add_parser("doctor", help="查看安装位置、依赖、数据目录和连接状态")
    doctor.add_argument("--json", action="store_true")
    doctor.add_argument("--port", type=port_number, default=8080)
    uninstall = sub.add_parser("uninstall", help="卸载 uv 安装的工具，并删除抓包、索引和证书")
    uninstall.add_argument("--keep-data", action="store_true", help="只卸载程序，保留数据")
    purge = sub.add_parser("purge", help="只删除当前数据目录的抓包、索引、报告和证书")
    for removal in (uninstall, purge):
        removal.add_argument("--dry-run", action="store_true", help="只预览将清理的路径")
        removal.add_argument("--yes", action="store_true", help="确认执行，跳过交互确认")
    args = parser.parse_args(argv)
    data_root = (args.data_dir or data_directory(legacy_root)).expanduser().resolve()
    defaults = {"file": data_root / "captures/codex.jsonl", "output": data_root / "captures/codex.jsonl",
                "index": data_root / "captures/monitor/index.sqlite3", "confdir": data_root / ".mitmproxy",
                "output_dir": data_root / "captures/analysis"}
    for key, value in defaults.items():
        if hasattr(args, key) and getattr(args, key) is None:
            setattr(args, key, value)
    prefix = (cli_prefix or [sys.executable, "-m", "codex_watch"]) + ["--data-dir", str(data_root)]
    if args.command == "doctor":
        return diagnose(args, data_root)
    if args.command in {"uninstall", "purge"}:
        from codex_watch.removal import run_removal
        import subprocess

        try:
            return run_removal(args, data_root)
        except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
            parser.error(str(exc))
    if args.command in {"watch", "search", "show"}:
        import json
        import sqlite3
        from codex_watch.monitor import ChatIndex, render_detail, render_search, run_watch

        try:
            if args.command == "watch":
                with data_locks(data_root, ("watch",)):
                    return run_watch(args, prefix)
            index = ChatIndex(args.index, readonly=True)
            try:
                if args.command == "search":
                    if not 1 <= args.limit <= 1000:
                        parser.error("--limit 必须在 1 到 1000 之间")
                    print(render_search(index.search(args.query, args.limit)))
                else:
                    chat = index.show(args.chat_id.removeprefix("#"), raw=args.raw)
                    print(json.dumps(chat, ensure_ascii=False, indent=2) if args.json or args.raw else render_detail(chat))
            finally:
                index.close()
        except (OSError, ValueError, sqlite3.Error) as exc:
            parser.error(str(exc))
        return 0
    if args.command == "analyze":
        from codex_watch.analysis import analyze, render_markdown, write_reports

        try:
            report = analyze(args.inputs or [data_root / "captures"], exclude_prewarm=args.exclude_prewarm)
            paths = write_reports(report, args.output_dir)
        except (OSError, UnicodeError, ValueError) as exc:
            parser.error(str(exc))
        if args.format == "json":
            import json
            print(json.dumps(report, ensure_ascii=False, indent=2))
        elif args.format == "markdown":
            print(render_markdown(report), end="")
        else:
            print(render_markdown(report).split("\n## call-", 1)[0])
            if report["warnings"]:
                print(f"存在 {len(report['warnings'])} 项解析/关联问题，详见报告。")
        print("报告: " + ", ".join(str(p) for p in paths), file=sys.stderr)
        return 0
    confdir = args.confdir.expanduser().resolve()
    if args.command == "serve":
        try:
            from mitmproxy.tools.main import mitmdump
        except ImportError:
            # Preserve the old source launcher, which may be run by system Python.
            if legacy_root is not None:
                python = Path(legacy_root) / (".venv/Scripts/python.exe" if os.name == "nt" else ".venv/bin/python")
                if python.is_file() and python.absolute() != Path(sys.executable).absolute():
                    command = [str(python), str(Path(legacy_root) / "codex_dump.py"), *(argv if argv is not None else sys.argv[1:])]
                    return run_client(command, os.environ.copy())
            parser.error("未安装 mitmproxy 依赖，请重新安装 codex-watch 或按 README 安装开发依赖")
        output = args.output.expanduser().resolve()
        command = [
            "-q", "-s", str(Path(__file__).with_name("addon.py")),
            "--listen-host", "127.0.0.1", "--listen-port", str(args.port),
            "--set", f"confdir={confdir}", "--set", "connection_strategy=lazy",
            "--set", f"capture_output={output}", "--set", f"capture_hosts={args.hosts}",
            "--set", f"capture_body={'true' if args.body else 'false'}",
            "--set", f"capture_max_bytes={args.max_body_bytes}",
        ]
        if args.upstream:
            if not args.upstream.startswith(("http://", "https://")):
                parser.error("--upstream 需要 http:// 或 https:// 代理地址")
            command += ["--mode", f"upstream:{args.upstream}"]
        print(f"启动抓包代理 127.0.0.1:{args.port}\nJSONL: {output}\nCtrl+C 停止。", flush=True)
        try:
            with data_locks(data_root, ("serve",)):
                confdir.mkdir(parents=True, exist_ok=True, mode=0o700)
                os.chmod(confdir, 0o700)
                return mitmdump(command) or 0
        except (OSError, ValueError) as exc:
            parser.error(str(exc))
    ca = confdir / "mitmproxy-ca-cert.pem"
    if not ca.is_file():
        parser.error(f"未找到 CA 证书 {ca}；请先在另一个终端运行 serve")
    try:
        with socket.create_connection(("127.0.0.1", args.port), timeout=2):
            pass
    except OSError:
        parser.error(f"无法连接 127.0.0.1:{args.port}；请先启动 serve 并检查端口")
    executable = shutil.which("codex")
    if not executable:
        parser.error("PATH 中未找到 codex，请先安装 Codex CLI")
    forwarded = args.codex_args
    if forwarded[:1] == ["--"]:
        forwarded = forwarded[1:]
    return run_client([executable, *forwarded], client_environment(args.port, ca))


def diagnose(args, data_root):
    import json

    try:
        version = importlib.metadata.version("mitmproxy")
    except importlib.metadata.PackageNotFoundError:
        version = None
    listening = False
    try:
        with socket.create_connection(("127.0.0.1", args.port), timeout=0.3):
            listening = True
    except OSError:
        pass
    details = {"version": __version__, "python": sys.version.split()[0], "executable": sys.executable,
               "package_dir": str(Path(__file__).resolve().parent), "data_dir": str(data_root),
               "mitmproxy": version, "codex": shutil.which("codex"),
               "ca_exists": (data_root / ".mitmproxy/mitmproxy-ca-cert.pem").is_file(),
               "port": args.port, "port_accepts_connections": listening}
    if args.json:
        print(json.dumps(details, ensure_ascii=False, indent=2))
    else:
        print(f"codex-watch {__version__} · Python {details['python']}\n程序：{details['package_dir']}\n数据：{data_root}")
        print(f"mitmproxy：{version or '未安装'}\nCodex：{details['codex'] or 'PATH 中未找到'}")
        print("证书：" + ("已生成" if details["ca_exists"] else "首次启动 watch / serve 时生成"))
        print(f"127.0.0.1:{args.port}：" + ("已有服务监听（未验证服务身份）" if listening else "未检测到监听"))
    return 0 if version else 1


if __name__ == "__main__":
    raise SystemExit(main())
