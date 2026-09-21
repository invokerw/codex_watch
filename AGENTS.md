# AGENTS.md

## 项目概览

这是一个 Python 3.12 项目，用于通过本机代理捕获和分析 Codex 请求。运行时代码位于 `codex_watch/`；根目录的 `codex_dump.py`、`codex_capture.py` 等文件是兼容源码启动器。`scripts/` 保存构建产物和安装包验收脚本。

## 目录约定

- 所有自动化测试放在 `tests/`，测试公共辅助代码也放在该目录中。
- 测试使用 `unittest`，测试数据应写入临时目录；不要依赖仓库里的真实 `captures/` 数据、证书或用户凭据。
- `captures/`、`.mitmproxy/`、虚拟环境和构建产物属于本地运行数据，不应提交到版本库。
- 修改测试布局时，同时检查 `pyproject.toml` 的源码归档清单、`scripts/verify_package.py`、CI 配置以及 `README.md` 和 `PACKAGING.md` 中的命令。

## 验证命令

在仓库根目录运行全部测试：

```bash
python3 -m unittest discover -s tests -p 'test_*.py' -v
```

集成测试会启动临时的本地 HTTPS/WebSocket 服务和 mitmproxy，不会访问真实 OpenAI API。构建或修改打包配置后，还应运行 `python3 -m build --outdir dist`，并按 `PACKAGING.md` 的说明执行安装包验收。

## 开发约定

- 保持 Python 3.12 兼容性，优先使用标准库；项目运行依赖固定为 `mitmproxy==11.1.3`。
- 捕获内容可能包含提示词、代码和工具输出。新增日志、fixture 或调试输出时必须继续遵守脱敏和文件权限约定。
- 修改命令行行为时同步更新相关文档和测试；避免在测试中调用真实网络服务或写入用户目录。
