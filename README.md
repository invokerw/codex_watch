# Codex 请求抓包工具

现已提供可安装的 **`codex-watch` 命令行工具**。本地安装、数据目录和跨平台验证说明见 [PACKAGING.md](PACKAGING.md)。安装版在任意目录运行，使用用户数据目录；下文 `python3 codex_dump.py ...` 命令使用源码目录内的数据。

实时查看 Codex 聊天的请求模型与返回模型是否一致，并按聊天内容找到对应抓包数据。底层使用本机 HTTP(S) 代理，支持 HTTP、SSE 和 WebSocket。

- 默认仅监听 `127.0.0.1:8080`，记录 `api.openai.com`、`chatgpt.com` 及其子域名。
- 推荐用 `watch`：自动启动代理、开启正文捕获并维护聊天索引。单独使用 `serve` 时默认只记录元数据，`--body` 开启正文预览。
- 凭据字段自动脱敏，包括 Authorization、Cookie、API key、常见 token 字段；支持嵌套 JSON 和 SSE 中的 JSON。
- HTTP 响应边到边转发，不会等 SSE 完成才交给 Codex。请求、完整 SSE 事件和 WebSocket 消息即时落盘，HTTP 响应摘要在结束或出错时落盘。
- 使用项目内的 CA 目录，日志权限设为 `0600`。包装命令只向所启动的进程传递代理和 CA 配置。

## 安装

使用 **Python 3.12**，当前依赖为 mitmproxy 11.1.3。源码目录中的 `/path/to/codex-watch` 示例需要替换为实际解压路径。

```bash
cd /path/to/codex-watch
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

完成上述依赖安装后，可使用下面的源码启动命令。Windows 的虚拟环境解释器路径为 `.venv\Scripts\python.exe`；下面的路径示例使用 macOS / Linux 格式。

## 快速开始

**终端 A：启动实时监视**：

```bash
cd /path/to/codex-watch
python3 codex_dump.py watch
```

自动启动项目 `.venv` 中的代理，已有日志会先导入索引，随后只显示新聊天。默认隐藏预热、标题生成和系统辅助请求。每次用户聊天会获得一个固定编号，例如：

```text
[发送] #abc123def456  hi  请求=gpt-6-astra
[一致] #abc123def456  gpt-6-astra → gpt-6-astra  [completed]
```

返回事件中首次出现不同模型时，立即显示 `[模型不一致]`；同一线程下一次请求改用了其他模型时，单独显示 `[请求模型变化]`，不推断变化原因。字段缺失显示“未知”。安全机制给出的更快模型候选不会被当作已经换模。

按 Ctrl+C 停止监视及它启动的代理，索引保留。日志追加到 `captures/codex.jsonl`，证书在 `.mitmproxy/`。

**终端 B：通过代理启动 Codex**，先切换到你希望 Codex 工作的项目目录：

```bash
cd /path/to/your/project
/path/to/codex-watch/.venv/bin/python \
  /path/to/codex-watch/codex_dump.py run
```

原有 Codex 参数放在 `--` 后面，例如：

```bash
/path/to/codex-watch/.venv/bin/python \
  /path/to/codex-watch/codex_dump.py run -- exec "说明这个项目的目录结构"
```

`run` 保留当前工作目录和 Codex 参数，设置大小写两组 `HTTP_PROXY` / `HTTPS_PROXY` / `ALL_PROXY`，将 `NO_PROXY` 限定为本机地址，并设置 `CODEX_CA_CERTIFICATE`。Codex 的 HTTPS、登录和安全 WebSocket 客户端支持该 CA 变量，见 [OpenAI 官方环境变量文档](https://developers.openai.com/zh-Hans/docs/config-file/environment-variables)。

**按内容找回聊天和原始数据**，可以在监视期间从另一个终端查询：

```bash
cd /path/to/codex-watch
python3 codex_dump.py search "hi"
python3 codex_dump.py search                   # 最近 20 条
python3 codex_dump.py show abc123def456         # 完整输入、回复、模型及来源行号
python3 codex_dump.py show abc123def456 --json  # 结构化结果，含安全状态证据
python3 codex_dump.py show abc123def456 --raw   # 同时输出对应的原始抓包事件
```

搜索同时匹配用户输入与回复，支持中文，按不区分大小写的连续文本匹配。`show` 支持唯一编号前缀。同一 `thread_id + turn_id` 下的工具调用归入同一条聊天；缺少这两个字段时按请求分别编号。列表只展示内容摘要，详情保留完整的已捕获内容。

**已有代理或只处理历史日志**：

```bash
python3 codex_dump.py watch --attach          # 监听已有 JSONL，不另开代理
python3 codex_dump.py watch --once            # 导入已有 JSONL 后退出
python3 codex_dump.py watch --file captures/other.jsonl
python3 codex_dump.py watch --upstream http://127.0.0.1:7890
```

`--attach` 要求原代理已经开启正文捕获；监听旧版本代理时，HTTP 流可能要结束后才能被索引。已运行的 Codex 进程不会自动改走代理，需要通过上面的 `run` 命令重新启动发出请求的 CLI。

索引默认位于 `captures/monitor/index.sqlite3`，`watch/search/show` 均可用 `--index` 指定其他位置。支持断点续读、未写完的行、日志轮换与事件去重，重启后可继续关联尚未完成的请求。每个索引同时只允许一个 `watch` 写入，但允许多个查询命令读取。

索引保存聊天文字、回复及压缩后的相关原始事件；即使原 JSONL 被轮换，仍能用 `show --raw` 回查已归档数据。这里的“原始事件”是抓包程序输出的已脱敏事件，不是未脱敏的网络字节。数据库权限为 `0600`。`watch` 默认每个正文最多检查 4 MiB，超限或原抓包没有正文时，未捕获的内容无法补回。

模型一致性只比较实际捕获的模型字段。名称差异也可能来自模型别名或版本差异，不能据此验证后台实际运行的模型权重。需要安全等待、更快模型重试的完整汇总时，使用下面的 `analyze`。

## 分析聊天请求、模型和安全等待

直接分析当前 `captures` 目录：

```bash
python3 codex_dump.py analyze
```

分析命令只依赖 Python 标准库，无需启动代理或登录账号。它会在终端输出汇总表，并生成：

- `captures/analysis/chat-analysis.md`：中文表格、每次调用的等待提示说明和原始日志位置。
- `captures/analysis/chat-analysis.json`：完整的机器可读结果，包含模型与安全状态的逐条证据。

也可以指定文件、隐藏预热调用或调整报告目录：

```bash
python3 codex_dump.py analyze captures/codex.jsonl
python3 codex_dump.py analyze captures --exclude-prewarm
python3 codex_dump.py analyze captures --output-dir captures/my-report
python3 codex_dump.py analyze captures --format json
```

目录模式读取当前层的 `.jsonl`、`.json` 和 `dump` 文件，支持 JSON 数组及连续的格式化 JSON 对象。相同的导出事件按内容去重，所以 `codex.jsonl` 和它的格式化 `dump` 不会重复计数。报告目录不会被递归读取。跨文件分割的同一连接应按捕获顺序提供输入。

分析范围为 `/responses` 和 `/chat/completions` 请求；模型列表、插件、用量等接口会跳过。WebSocket 握手本身不算模型调用，每条 `response.create` 单独分析；响应通过同一 flow 和 `response.id` 关联。无法确定对应关系的并发事件会记录问题，避免猜测配对。支持普通 JSON 响应和 SSE。

| 报告信息 | 判定方式 |
| --- | --- |
| 请求 / 返回模型 | `request_model` 与 `response_models` 精确比较；`response_model` 是最后观察到的返回模型，所有阶段的证据在 `model_observations` 中。字段缺失记为未知。 |
| 调用用途 | `generate=false` 或 `request_kind=prewarm` 记为预热；仅要求 `title` 字段的输出结构记为标题生成推断；同时保留 `thread_source`。 |
| 安全机制启用 | 来自 `x-codex-safety-buffering-enabled`；缺失记为未知。 |
| 等待状态 / 提示 | 统计事件中的 `safety_buffering` 布尔值；任一 true 记为已报告等待，只有 false 记为未观察到，缺失记为未知。对象形式的详情单独保留，不把非空对象直接当作 true。 |
| 更快模型候选 | 来自 `x-codex-safety-buffering-faster-model` 或 `retry_model`，保留来源位置。 |
| 疑似更快模型重试 | 同线程相邻的非预热请求、相同输入、相同用途、请求模型变为先前提供的候选，且不是通过 `previous_response_id` 继续回复。 |

“同线程模型已变化”只表示观察到模型变化，原因未知；“疑似更快模型重试”也不证明用户点击了按钮。跨线程的新会话不会仅因模型等于候选值就被归为重试。网络抓包无法确认界面是否显示等待提示，这一项在 JSON 中保留为 `null`。

`enabled=true` 与事件的 `safety_buffering=false` 可以同时存在。字段缺失不当作 false。报告中的中文等待提示由分析器根据状态生成，不是从客户端界面抓取的文案。耗时是请求到响应结束的时间，不是安全处理耗时。

模型比较仅覆盖实际捕获的字段：不完整、失败或缺失正文的请求要同时查看 `status`、`issues` 和 `warnings`。精确模型名差异可能是别名/版本差异，不能单独证明后台使用了其他模型权重。报告不导出提示词、代码、正文或认证头；输出文件权限为 `0600`。

## 配置

```bash
# 只保存元数据
.venv/bin/python codex_dump.py serve

# 自定义端口和输出文件；run 也需要指定 --port 8899
.venv/bin/python codex_dump.py serve --port 8899 --body --output captures/debug.jsonl
.venv/bin/python codex_dump.py run --port 8899

# 需要通过已有代理联网时，形成 Codex → 抓包代理 → 上游代理 的链路
.venv/bin/python codex_dump.py serve --body --upstream http://127.0.0.1:7890

# 使用自定义 API 域名时扩展记录范围
.venv/bin/python codex_dump.py serve --body --hosts api.openai.com,chatgpt.com,api.example.com

# 空字符串表示记录所有经过代理的域名
.venv/bin/python codex_dump.py serve --body --hosts ''

# 每个正文默认最多检查 1 MiB 解码内容，可以调整
.venv/bin/python codex_dump.py serve --body --max-body-bytes 4194304
```

`--hosts` 是日志过滤器；所有送入代理的 HTTPS 连接仍会由 mitmproxy 处理。它不按进程身份区分流量，Codex 启动的子进程也可能继承代理环境。

`--confdir` 可指定其他证书目录，`serve` 和 `run` 应使用同一个目录。若已有企业 CA 配置，可手动准备包含所需 CA 的 PEM 包，再按下方方式启动。

## 不使用包装命令

先启动代理，再在另一个终端执行：

```bash
HTTPS_PROXY=http://127.0.0.1:8080 \
HTTP_PROXY=http://127.0.0.1:8080 \
ALL_PROXY=http://127.0.0.1:8080 \
NO_PROXY=localhost,127.0.0.1,::1 \
CODEX_CA_CERTIFICATE=/path/to/codex-watch/.mitmproxy/mitmproxy-ca-cert.pem \
codex
```

也可直接加载 addon（无需包装器）：

```bash
.venv/bin/mitmdump -q -s ./codex_capture.py \
  --listen-host 127.0.0.1 --listen-port 8080 \
  --set confdir=./.mitmproxy \
  --set capture_output=./captures/codex.jsonl \
  --set capture_hosts=api.openai.com,chatgpt.com \
  --set capture_body=true \
  --set capture_max_bytes=1048576
```

这里使用的自定义选项均为 `capture_*`，以避免与 mitmproxy 自身选项冲突。

## 输出格式

每行一个事件，`flow_id` 关联同一次连接上的请求、响应和 WebSocket 消息，`schema_version` 为 `2`：

| event | 含义 | 主要字段 |
| --- | --- | --- |
| `request` | 请求已收到 | `method`、`url`、`request.headers`、`request.body` |
| `response` | HTTP 响应结束或 WebSocket 握手 | `response.status_code`、`response.body`、`duration_ms` |
| `sse_event` | 流中的完整 JSON 事件或 `[DONE]`，开启正文捕获时写入 | `index`、`body.json`、`response_headers` |
| `error` | 请求或传输失败 | `error`，可能含已收到的部分 `response` |
| `websocket_message` | 一条完整 WebSocket 消息 | `index`、`direction`、`body` |
| `websocket_end` | WebSocket 连接关闭 | `close_code`、`close_reason` |

请求、响应和 WebSocket 正文对象的 `bytes` 和 `sha256` 对应收到的完整正文原始字节（压缩正文在解压前计算；WebSocket 按消息 payload 计算）。`captured` 表示是否保存预览，预览位于 `json`、`text` 或 `form` 字段；HTTP 响应摘要中的 SSE 位于 `text`。`truncated` 表示达到解码内容上限，`omitted` 解释为何省略正文。

`sse_event` 在配置的正文上限内逐条解码并写入，不包含整个 HTTP 正文的大小和哈希；`[DONE]` 记为 `{"type":"capture.stream_done"}`。分析器已读到实时事件时，不会重复计算最终 HTTP 摘要中的相同内容。

正文预览支持 UTF-8、JSON、表单、SSE，以及 gzip / zlib 格式 deflate 解压。超限或解析失败的 JSON 会省略正文，SSE 仅保留完整事件；非 UTF-8、二进制、multipart、Brotli / Zstandard 等暂不保存预览，仍记录大小和哈希并正常转发。WebSocket 二进制消息仅保存元数据。响应预览内存有上限；mitmproxy 本身仍会缓冲请求和完整 WebSocket 消息。

脱敏是尽力匹配常见凭据，并不能识别所有敏感内容；开启 `--body` 后会保存提示词、代码和工具输出。日志和 CA 私钥已加入 `.gitignore`。

## 排查

- **没有记录**：检查代理是否启动、端口是否一致、目标域名是否在 `--hosts` 中，以及是否设置了绕过代理的 `NO_PROXY`。
- **证书错误**：确认 `CODEX_CA_CERTIFICATE` 指向本次代理生成的 `mitmproxy-ca-cert.pem`，以及所用 Codex 版本支持该变量。上游企业代理的信任配置需要另行提供给 mitmproxy。
- **连接不到 OpenAI**：如果平时需要网络代理，使用 `serve --upstream ...` 指向已有 HTTP 代理，避免把上游地址设成抓包代理自身。
- **只看到请求、暂时没有响应事件**：SSE 的 `response` 摘要在流结束时写入；开启正文捕获后，流中的完整事件会先作为 `sse_event` 写入。强制终止代理可能留下未完成请求。
- **桌面应用、已有 app-server、远程会话**：此包装命令面向新启动的本机 CLI。已运行的进程不会自动继承环境变量；真正发出网络请求的进程需要从带代理和 CA 配置的环境启动。远程服务器上的请求不能通过本机包装命令自动抓取。

## 验证

核心测试仅使用 Python 标准库；集成测试需要已安装的 mitmproxy 依赖，并会监听临时本地端口：

```bash
python3 -m unittest -v test_capture_core
python3 -m unittest -v test_capture_analysis
python3 -m unittest -v test_chat_monitor
.venv/bin/python -m unittest -v test_proxy_integration
```

核心测试覆盖内容索引、聊天归组、模型比较、断点恢复、日志轮换及读写并行。集成测试生成临时 CA 和 HTTPS 服务，验证真实 HTTPS 代理、压缩 JSON、SSE 完成前的模型告警和索引、WebSocket 双向消息、域名过滤、上游断连，以及启动器、包装命令和 `watch` 的代理退出清理。不会调用真实 OpenAI API，也不需要账号或 API key；测试结束后停止服务并清理临时文件。

流式转发和 WebSocket hooks 的实现参考 [mitmproxy 官方 addon 示例](https://docs.mitmproxy.org/stable/addons/examples/)。
