# AIPerf Desktop（本地离线版）

AIPerf 压测可视化的 Windows / macOS 桌面客户端。安装包内置便携 Python、aiperf 0.13.0 及全部依赖、
WebUI 和默认 Qwen tokenizer，**运行期无需联网**，可在离线局域网内直接压测 OpenAI 兼容的 LLM 服务。

## 结构

```
engine/                 本地引擎源码（打包进安装包）
  app.py                Web 引擎（仅监听 127.0.0.1 随机端口）
  static/index.html     WebUI
  tokenizers/qwen3.8-27b  内置默认 tokenizer
ui/index.html           启动加载页
src-tauri/              Tauri v2 外壳（Rust 启动器：拉起引擎 → 健康检查 → 开窗 → 退出清理）
scripts/
  prepare_engine.ps1    构建期：下载便携 Python + pip 安装 aiperf + 拷贝引擎
  smoke_test.py         构建期：对 mock LLM 服务跑一次真实 aiperf 压测
  mock_openai_server.py 冒烟测试用的 OpenAI 兼容流式服务
.github/workflows/build-windows.yml   Windows 安装包 CI
app-icon.svg            图标源文件（CI 中用 `tauri icon` 生成全平台图标）
```

## Windows 构建（GitHub Actions）

推送到 `main` 或手动触发 workflow → 产物 `AIPerf-Desktop-windows-x64`（NSIS 安装包）。
推送 `v*` 标签会自动发布到 GitHub Release。

流程：准备引擎 → **冒烟测试（真实 aiperf 压测必须通过）** → 生成图标 → `tauri build --bundles nsis`。

## 运行时

- 数据目录：`%APPDATA%\ai.easwift.aiperf-desktop\`（历史记录 `runs/`、引擎日志 `engine.log`）
- 离线：tokenizer 默认使用内置本地目录，aiperf 不访问 HuggingFace Hub（界面可改为本机其他 tokenizer 目录；
  填 Hub 名称则需联网）。**不要设置 `HF_HUB_OFFLINE`**：aiperf 0.13 在该模式下会拒绝本地 tokenizer 目录。
  冒烟测试在“外网不可达”环境下运行，以此验证离线可用
- 安装包内嵌 WebView2 离线安装器，未装 WebView2 的离线机器也能安装
- Windows 下“取消测试”为直接结束进程树（无优雅中断）
