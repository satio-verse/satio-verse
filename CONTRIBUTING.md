# 参与贡献

先读 [docs/开发者指南.md](docs/开发者指南.md)（如果存在）与 [docs/许可证与商标说明.md](docs/许可证与商标说明.md)。

## 开发环境

```bash
python -m venv .venv && . .venv/Scripts/activate   # Windows
pip install -r requirements.txt
python -m wm.services up        # 起本机服务（8790 服务端 / 8784 模型 / 9880 语音）
python -m pytest -q tests        # 跑测试
```

不想装模型也能跑：`config/local.json` 里把引擎切成 mock，流程与仪表照样能通。

## 提交前自检

1. `python -m compileall -q wm tools`（改 Python 时）
2. `node --check desktop/web/satio.js`（改前端时；ESM 需先复制成 .mjs 再 check）
3. 相关测试：`python -m pytest -q tests/test_<模块>.py`
4. 不要把 `config/local.json`、模型权重、日志、私钥提交上来

## 插件 / 世界贡献

- 插件包用 `wmworld.json`（schema 2）声明，字段见 `wm/agent/plugin_manifest.py` 与 `plugins/` 里的样例
- 世界插件必须声明 `world.kind`（`chat` / `space` / `game`）、`prefix`、动作名 `<prefix>_<动词>`、事件名
- 打包与自检：`python tools/package_plugin.py --dir <你的插件目录>`（若有），或手动按 `docs/` 里的格式打包
- 市场提交走 `satio-plugins` 仓库的 Pull Request，CI 会跑契约校验

## 代码风格

- Python：3.11+ 语法，类型标注能写就写；不要引入未在 `requirements.txt` 声明的依赖
- 前端：原生 ES modules，不引入构建步骤（本项目刻意保持零打包）
- 注释与文档用中文，变量名用英文

## 提交信息

`<area>: <一句话>`，例如 `tts: 打开 v5turbo 的 AR CUDA graph`。
