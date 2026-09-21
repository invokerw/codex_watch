# codex-watch 0.1.1

在本机监听经代理发出的 Codex 聊天请求，比较请求模型与返回模型，并按输入或回复内容查找对应抓包。支持 HTTP、SSE 和 WebSocket。

本版本以源码和 Python 安装包交付：`codex_watch-0.1.1-py3-none-any.whl`。源码仓库为 [invokerw/codex_watch](https://github.com/invokerw/codex_watch)。尚未制作自带 Python 的独立可执行程序，也未发布到 PyPI。

## 安装

从 GitHub 获取源码：

```bash
git clone https://github.com/invokerw/codex_watch.git
cd codex_watch
uv tool install --python 3.12 .
```

收到源码压缩包时，解压后在包含 `pyproject.toml` 的目录执行：

```bash
uv tool install --python 3.12 .
codex-watch doctor
codex-watch watch
```

然后在另一个终端的工作项目目录运行 `codex-watch run`。源码包不含依赖、聊天记录或证书；首次安装会获取依赖，首次启动代理会生成本机证书。

安装本地 wheel（路径替换为实际下载位置）：

```bash
uv tool install --python 3.12 /path/to/codex_watch-0.1.1-py3-none-any.whl
codex-watch --version
codex-watch doctor
```

若安装后找不到命令，执行 `uv tool update-shell` 并重新打开终端。升级本地包可以使用相同命令并加 `--force`。彻底卸载使用 `codex-watch uninstall`，会同时清理当前数据目录；详见下面的卸载说明。

本版本明确使用 Python 3.12 和已验证的 mitmproxy 11.1.3，安装器会获取其余依赖。wheel 本身不包含 Python 或依赖，首次安装通常需要联网。没有 uv 时，也可在 Python 3.12 虚拟环境中执行 `python -m pip install /path/to/codex_watch-0.1.1-py3-none-any.whl`。

## 使用

终端 A：

```bash
codex-watch watch
```

终端 B 切换到工作项目目录，然后执行：

```bash
codex-watch run
```

`watch` 自动启动代理、捕获正文和更新聊天索引；`run` 给新启动的 Codex CLI 配置代理和 CA。需要自行安装并登录 Codex。已有代理时用 `codex-watch watch --attach`。已有 Codex 进程不会自动改走代理。

```bash
codex-watch search "聊天内容"
codex-watch search
codex-watch show 聊天编号
codex-watch show 聊天编号 --raw
codex-watch watch --once --file /path/to/codex.jsonl
codex-watch analyze /path/to/captures
```

同一线程、同一轮聊天的请求归到同一个编号；返回事件出现不同模型时即时提示。模型名称差异不等于验证了后台实际使用的模型权重。

## 数据与配置

安装版的数据目录固定在当前用户下，与启动命令的工作目录无关：

| 系统 | 默认目录 |
| --- | --- |
| macOS | `~/Library/Application Support/codex-watch` |
| Windows | `%LOCALAPPDATA%\codex-watch` |
| Linux | `$XDG_DATA_HOME/codex-watch`，缺省为 `~/.local/share/codex-watch` |

目录中包含 `captures/codex.jsonl`、`captures/monitor/index.sqlite3`、报告和 `.mitmproxy` 证书目录。每台机器在首次启动代理时生成自己的 CA。抓包和索引包含已捕获的聊天内容；发布文件不包含用户抓包、数据库或证书。

可以设置环境变量 `CODEX_WATCH_HOME`，或在子命令前指定 `--data-dir`：

```bash
codex-watch --data-dir /path/to/my-data watch
codex-watch --data-dir /path/to/my-data run
codex-watch --data-dir /path/to/my-data search "关键词"
```

`watch` 和 `run` 必须使用相同的数据目录与端口。自定义端口示例：`codex-watch watch --port 8899` 与 `codex-watch run --port 8899`。需要上游 HTTP 代理时使用 `codex-watch watch --upstream http://127.0.0.1:7890`。

原有源码命令 `python3 codex_dump.py ...` 继续默认使用源码目录内的数据，不会移动或覆盖历史数据。安装版可通过 `--data-dir` 指向原项目目录，直接使用原索引。不要同时运行两个监视器更新同一索引。

## 卸载与清理

从 0.1.1 起，使用工具自己的卸载命令会默认同时清理数据：

```bash
codex-watch uninstall --dry-run    # 预览路径，不执行删除
codex-watch uninstall              # 显示路径，输入 DELETE 后执行
codex-watch uninstall --yes        # 已确认，跳过交互确认
```

会删除当前数据目录下的 `captures/`（含抓包、聊天索引、报告、代理日志）和 `.mitmproxy/`（证书），然后通过 uv 卸载当前工具环境。空数据目录会一并移除；同目录下的其他文件保留。清理抓包是不可恢复的文件删除，不保证存储介质上的安全擦除。

先退出正在运行的 watch / serve。新版进程和聊天索引锁会阻止清理；旧版独立 serve 或直接加载 addon 的代理需要自行停止。符号链接或联接点会中止清理，避免删除链接目标。

自定义数据目录时，卸载也要指定同一个目录：

```bash
codex-watch --data-dir /path/to/my-data uninstall
```

只清理选定目录；单独通过 `--file`、`--index`、`--confdir` 保存到其他位置的文件不会被搜索删除，原源码目录的 captures 也不会自动被安装版清理。

其他选择：

```bash
codex-watch uninstall --keep-data  # 卸载程序，保留数据
codex-watch purge                  # 清理数据，保留程序；适用于源码和 pip 安装
uv tool uninstall codex-watch      # uv 自身的卸载命令仍只移除程序
```

自动卸载会验证当前程序确实来自该 uv 工具环境，避免误卸载另一个安装。对于源码或普通虚拟环境安装，先 `purge`，再用原安装方式卸载程序。若数据清理成功但 uv 卸载失败，会明确报告部分完成的状态。

## 构建与验证

从源码目录构建 wheel 和源码归档：

```bash
uv build --python 3.12 --out-dir dist
python3 -m unittest -v test_capture_core test_capture_analysis test_chat_monitor test_packaging test_removal
.venv/bin/python -m unittest -v test_proxy_integration
```

验证安装包时，在独立环境安装后运行以下脚本。脚本把测试复制到临时目录，移除 `PYTHONPATH`，确认程序从新环境的 `site-packages` 加载，再执行全部测试：

```bash
uv venv --python 3.12 .package-test/venv
uv pip install --python .package-test/venv/bin/python dist/codex_watch-0.1.1-py3-none-any.whl
python3 scripts/verify_package.py --python .package-test/venv/bin/python
python3 scripts/verify_uninstall.py --wheel dist/codex_watch-0.1.1-py3-none-any.whl --python .package-test/venv/bin/python --cache-dir .build-cache
```

卸载验收只使用临时的 uv 工具目录、命令目录和模拟数据，分别验证默认删除数据与 `--keep-data`，不会卸载用户已安装的工具。该脚本使用离线缓存，运行前需要安装依赖以填充指定缓存目录。

以上虚拟环境路径示例用于 macOS / Linux；Windows 的解释器位于 `.package-test\venv\Scripts\python.exe`。

构建使用明确的文件包含列表；wheel 只包含 `codex_watch` 程序包和分发元数据。源码归档额外包含文档、兼容启动器和测试。

本地验收需在新的 Python 3.12 环境安装 wheel，并从项目外运行测试，验证资源加载、代理启动、模型告警、检索和进程退出。测试只连接临时本地服务，不调用真实 OpenAI API。验证结果见交付目录中的 `VALIDATION.md`。

已为用户目录、文件锁和进程管理增加 macOS / Linux / Windows 分支。当前机器是 macOS Apple Silicon；Windows 和 Linux 仍需在对应系统做集成验证，不能据此声称所有平台已经通过。

打包方式参考 [Python Packaging 官方指南](https://packaging.python.org/en/latest/tutorials/packaging-projects/)，工具安装参考 [uv 文档](https://docs.astral.sh/uv/concepts/tools/)。
