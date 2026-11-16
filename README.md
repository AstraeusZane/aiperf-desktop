# AIPerf Desktop · LLM 推理压测可视化客户端（本地离线版）

面向 Windows / macOS 的大模型推理服务压测桌面客户端。在界面上填写参数即可对 **OpenAI 兼容接口**的 LLM 服务
（vLLM、SGLang、TensorRT-LLM 等）发起压测，测试结束自动解析报告，展示 TTFT、Prefill/Decode 速率、分位数时延等指标。

安装包**内置完整压测引擎**（便携 Python + NVIDIA AIPerf + 全部依赖 + 默认 tokenizer），
**无需安装 Python、无需部署服务端、运行期无需联网**，离线局域网内即装即用。

---

## 一、压测后端（开源项目与版本）

本客户端**不自研压测逻辑**，所有请求发送、计时与统计均由 NVIDIA 官方开源压测工具 **AIPerf** 完成，
客户端只负责参数界面、任务调度与报告展示。

| 项目 | 说明 |
|---|---|
| 后端名称 | **NVIDIA AIPerf**（NVIDIA GenAI-Perf 的后继项目） |
| 开源地址 | **https://github.com/ai-dynamo/aiperf** |
| 内置版本 | **aiperf 0.13.0**（PyPI：`aiperf==0.13.0`，版本固定，随安装包发布） |
| 开源协议 | Apache-2.0 |
| 调用方式 | `python -m aiperf profile ...`（界面参数一一映射为 aiperf 命令行参数，完整命令可在运行日志首行查看） |

其他内置组件：

| 组件 | 版本 / 来源 |
|---|---|
| Python 运行时 | CPython 3.12（[python-build-standalone](https://github.com/astral-sh/python-build-standalone) 便携版） |
| 默认 tokenizer | Qwen（`engine/tokenizers/qwen3.8-27b`，13 MB） |
| 桌面外壳 | [Tauri v2](https://tauri.app)（系统 WebView：Windows WebView2 / macOS WKWebView） |

> 升级后端：修改 `scripts/prepare_engine.ps1` 与 `scripts/prepare_engine.sh` 中的 `AiperfVersion` / `AIPERF_VERSION`，
> 发布新版本安装包即可。

---

## 二、下载

前往 **[Releases 页面](https://github.com/AstraeusZane/aiperf-desktop/releases/latest)** 下载最新版本：

| 平台 | 安装包 | 大小 |
|---|---|---|
| Windows 10/11 x64 | `AIPerf.Desktop_<版本>_x64-setup.exe` | 约 330 MB |
| macOS 11+ Apple Silicon（M1/M2/M3/M4） | `AIPerf.Desktop_<版本>_aarch64.dmg` | 约 210 MB |

v0.1.0 直链：
[Windows](https://github.com/AstraeusZane/aiperf-desktop/releases/download/v0.1.0/AIPerf.Desktop_0.1.0_x64-setup.exe) ·
[macOS](https://github.com/AstraeusZane/aiperf-desktop/releases/download/v0.1.0/AIPerf.Desktop_0.1.0_aarch64.dmg)

> 暂不提供 Intel Mac 版本。

---

## 三、安装

### Windows

1. 双击 `AIPerf.Desktop_x.x.x_x64-setup.exe`，按向导安装（默认装到当前用户，无需管理员权限）。
2. 安装包**未做代码签名**，若出现「Windows 已保护你的电脑」（SmartScreen）：点击 **更多信息 → 仍要运行**。
3. 安装包已内置 WebView2 离线安装器，离线机器也可直接安装。

**CPU 要求**：需支持 x86-64-v2 指令集（SSE4.2 / POPCNT，2009 年后的 Intel/AMD 处理器均支持）。
**虚拟机**（Proxmox / KVM / QEMU）若使用默认 CPU 类型 `kvm64` / `qemu64`，会报错
`NumPy was built with baseline optimizations (X86_V2) but your machine doesn't support (X86_V2)`，
请将虚拟机 CPU 类型改为 **`host`** 或 **`x86-64-v2-AES`** 后重启。

### macOS（信任操作）

1. 打开 `.dmg`，将 **AIPerf Desktop** 拖入「应用程序（Applications）」文件夹。
2. 应用**未经 Apple 开发者证书签名与公证**，从浏览器下载后首次打开通常会被 Gatekeeper 拦截，按以下任一方式处理：

**方式 A（推荐）：终端解除隔离属性**

如提示「“AIPerf Desktop”已损坏，无法打开。你应该将它移到废纸篓」，打开「终端」执行一次：

```bash
xattr -cr "/Applications/AIPerf Desktop.app"
```

然后正常双击打开即可。（该命令仅移除浏览器下载时附加的“隔离”标记，不修改应用内容。）

**方式 B：系统设置中放行**

如提示「无法打开，因为无法验证开发者」：

1. 点「完成」关闭提示；
2. 打开 **系统设置 → 隐私与安全性**，在页面下方「安全性」处找到 AIPerf Desktop 的提示，点击 **仍要打开**；
3. 输入登录密码确认，再次打开应用时点 **打开**。

> macOS 15 及以上已移除“右键 → 打开”的放行方式，请使用以上方式。

---

## 四、使用

1. 启动应用（首次启动需 10–30 秒加载引擎，之后更快）。顶部显示「本地引擎运行中 · 离线可用」即就绪。
2. 填写**目标服务**：
   - **服务地址**：base URL，例如 `http://192.168.1.10:8000`，**末尾不要带 `/v1`**（aiperf 会自动拼接 `/v1/chat/completions`）
   - **API Key**：服务未开启鉴权可留空
   - **Endpoint 类型**：`chat` 或 `completions`
   - **模型名称**：点「↻ 获取模型」从服务的 `/v1/models` 拉取后下拉选择
   - **/metrics 地址**（可选）：vLLM 的 Prometheus 指标地址，如 `http://192.168.1.10:8000/metrics`。填写后可得到服务端 Prefill/Decode 阶段速率
   - **Tokenizer 路径**：默认内置 Qwen tokenizer；压测其他模型时建议改为该模型 tokenizer 所在的**本机目录**
3. 填写**负载**：并发数、请求数、输入/输出长度（均值与标准差）、语料类型、extra-inputs（默认 `ignore_eos:true`，保证输出长度固定）。
   也可点击预设「默认（4096/512 × 20）」或「快速验证（256/32 × 2）」。
4. 点 **▶ 发起测试**，下方实时显示运行日志；结束后自动展示结果，并写入「历史记录」。

### 语料类型

| 选项 | 内容 |
|---|---|
| 文本（默认） | 莎士比亚文本，aiperf 默认合成语料 |
| 代码 | 代码片段、bash 输出、JSON、报错栈、git diff 等 |
| 随机 token | 从词表随机抽取，对齐 vLLM bench RandomDataset |

### 指标口径

| 指标 | 来源 / 计算 |
|---|---|
| Prefill (P) 服务端速率 | `vllm:prompt_tokens_by_source{source=local_compute}` ÷ `vllm:request_prefill_time_seconds` 总和（需填 /metrics） |
| Decode (D) 服务端速率 | `vllm:generation_tokens` ÷ `vllm:request_decode_time_seconds` 总和（需填 /metrics） |
| TTFT、请求耗时、P50/P95、错误率 | AIPerf 客户端报告 `profile_export_aiperf.json` |
| 客户端 Prefill 估算 | 平均输入长度 ÷ 平均 TTFT |
| 客户端单用户 Decode | AIPerf `output_token_throughput_per_user` |

> 服务端速率基于测试时间窗内 /metrics 计数器的增量，若同一服务上有其他流量会被一并统计。
> 压测数据从**本机**发出，建议客户端与被测服务处于同一局域网，否则 TTFT 会包含网络延迟。

### 导入已有报告

在「导入已有报告目录」中填写包含 `profile_export_aiperf.json` 的目录（例如命令行跑 aiperf 产出的 artifacts 目录），即可在界面中查看。

---

## 五、数据与日志

| 内容 | Windows | macOS |
|---|---|---|
| 历史记录 / 原始报告 | `%APPDATA%\ai.easwift.aiperf-desktop\runs\` | `~/Library/Application Support/ai.easwift.aiperf-desktop/runs/` |
| 引擎日志 | `%APPDATA%\ai.easwift.aiperf-desktop\engine.log` | `~/Library/Application Support/ai.easwift.aiperf-desktop/engine.log` |

每次测试的 aiperf 原始产物（`profile_export_aiperf.json/csv`、`server_metrics_export.*`、`inputs.json`、`logs/aiperf.log`）
保存在 `runs/<任务ID>/artifacts/` 下。卸载应用不会删除该目录。

---

## 六、常见问题

| 现象 | 原因与处理 |
|---|---|
| 启动后窗口显示「本地引擎启动失败」 | 查看窗口中的日志或 `engine.log`；将日志发给维护者 |
| 报错 `NumPy ... doesn't support (X86_V2)` | CPU 指令集不足，多见于虚拟机，见「安装 → Windows」 |
| macOS 提示「已损坏，无法打开」 | 未签名应用被 Gatekeeper 拦截，执行 `xattr -cr` 命令，见「安装 → macOS」 |
| 测试失败、日志中出现 404 | 服务地址末尾多写了 `/v1` |
| 获取模型失败 | 检查服务地址与 API Key；本机能否访问该地址 |
| 填写 HuggingFace 模型名作为 tokenizer 失败 | 离线环境无法下载，请改为本机 tokenizer 目录 |
| Windows 上取消测试 | 直接结束 aiperf 进程树（macOS 为 SIGINT 优雅退出） |

---

## 七、开发与构建

### 项目结构

```
engine/                       本地引擎源码（打包进安装包）
  app.py                      Web 引擎：仅监听 127.0.0.1 随机端口，纯 Python 标准库
  static/index.html           WebUI
  tokenizers/qwen3.8-27b/     内置默认 tokenizer
ui/index.html                 启动加载页
src-tauri/                    Tauri v2 外壳（Rust 启动器：拉起引擎 → 健康检查 → 开窗 → 退出时清理进程）
scripts/
  prepare_engine.ps1          Windows 构建期：下载便携 Python + 安装 aiperf + 拷贝引擎
  prepare_engine.sh           macOS 构建期：同上（aarch64-apple-darwin）
  smoke_test.py               构建期冒烟测试：对 mock 服务跑一次真实 aiperf 压测（外网不可达环境下）
  mock_openai_server.py       冒烟测试用的 OpenAI 兼容流式服务
.github/workflows/
  build-windows.yml           Windows 安装包（NSIS）
  build-macos.yml             macOS Apple Silicon 安装包（.app + .dmg）
app-icon.svg                  图标源文件（CI 中用 `tauri icon` 生成全平台图标）
```

### CI 构建

两个 workflow 在推送到 `main` 或手动触发时运行，流程一致：

准备引擎 → **冒烟测试（真实 aiperf 压测必须通过，否则不出包）** → 生成图标 → `tauri build` → 上传 Artifacts

### 发布新版本

1. 修改 `src-tauri/tauri.conf.json` 中的 `version`，提交并推送；
2. 在 GitHub 上**先创建** Release（标签 `vX.Y.Z`），创建标签会触发两个 workflow，构建完成后安装包自动附加到该 Release。

> 若直接推送标签而不预先创建 Release，两个 workflow 可能同时创建 Release 导致其中一个失败。

### 离线说明

运行期 tokenizer 使用本地目录，aiperf 不访问 HuggingFace Hub。
**不要设置 `HF_HUB_OFFLINE`**：aiperf 0.13.0 在该模式下只查找 HF 缓存，会拒绝本地 tokenizer 目录。
冒烟测试将所有外网 HTTP 指向不可达代理运行，以此验证离线可用。

---

## 致谢

- [NVIDIA AIPerf](https://github.com/ai-dynamo/aiperf)（Apache-2.0）—— 压测后端
- [Tauri](https://tauri.app)、[python-build-standalone](https://github.com/astral-sh/python-build-standalone)
