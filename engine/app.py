#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AIPerf Web Dashboard — 纯 Python 标准库实现（零第三方依赖）。

功能：
  1. 页面表单填参数 → 后台调用 /root/venv/bin/aiperf profile 发起压测
  2. 实时回传运行日志（轮询）
  3. 测试结束后解析 profile_export_aiperf.json + server_metrics_export.json，
     按仪表盘口径输出指标（与截图一致的 Prefill/Decode 服务端速率算法）
  4. 历史记录、取消测试、导入已有报告目录
"""

import argparse
import json
import os
import re
import shutil
import signal
import subprocess
import threading
import time
import urllib.request
import uuid
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import sys

IS_WIN = os.name == "nt"
HERE = Path(__file__).resolve().parent
BASE = Path(os.environ.get("AIPERF_WEB_HOME", "/root/aiperf-web"))
RUNS_DIR = BASE / "runs"
STATIC_DIR = Path(os.environ.get("AIPERF_WEB_STATIC", str(HERE / "static")))
# 桌面版：用内置 Python 以 `python -m aiperf` 方式调用（规避 Windows 下 Scripts/*.exe 启动器不可重定位的问题）
AIPERF_PYTHON = os.environ.get("AIPERF_PYTHON", "")
AIPERF_BIN = os.environ.get("AIPERF_BIN", "/root/venv/bin/aiperf")
AIPERF_CMD = [AIPERF_PYTHON, "-m", "aiperf"] if AIPERF_PYTHON else [AIPERF_BIN]
DEFAULT_TOKENIZER = os.environ.get("AIPERF_DEFAULT_TOKENIZER", "/root/qwen3.8-27b")
DESKTOP = os.environ.get("AIPERF_WEB_DESKTOP") == "1"
DEFAULT_PORT = int(os.environ.get("AIPERF_WEB_PORT", "8099"))

# Windows：子进程不弹控制台窗口
_WIN_FLAGS = (getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)) if IS_WIN else 0


def kill_tree(pid, graceful=False):
    """跨平台结束进程树。"""
    if IS_WIN:
        # 无控制台的子进程收不到 CTRL_BREAK，统一用 taskkill 结束整棵进程树
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    else:
        try:
            os.killpg(os.getpgid(pid), signal.SIGINT if graceful else signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass

_LOCK = threading.Lock()
_RUNS = {}  # rid -> dict(state, proc, log_fh)


# ----------------------------------------------------------------------------
# 工具函数
# ----------------------------------------------------------------------------
def now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def fmt_int(v):
    try:
        return f"{round(float(v)):,}"
    except (TypeError, ValueError):
        return "—"


def fmt_dec(v, nd=1):
    try:
        s = f"{float(v):,.{nd}f}"
        return s
    except (TypeError, ValueError):
        return "—"


def fmt_smart(v, nd=1):
    """>=1000 取整加千分位，否则保留 nd 位小数。"""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return "—"
    if abs(f) >= 1000:
        return f"{round(f):,}"
    return f"{f:,.{nd}f}"


def fmt_ms(v):
    return f"{fmt_int(v)} ms"


def run_dir(rid):
    return RUNS_DIR / rid


def state_path(rid):
    return run_dir(rid) / "state.json"


def save_state(rid):
    with _LOCK:
        d = run_dir(rid)
        d.mkdir(parents=True, exist_ok=True)
        st = _RUNS.get(rid, {})
        data = {k: v for k, v in st.items() if k != "proc" and k != "log_fh"}
        state_path(rid).write_text(json.dumps(data, ensure_ascii=False, indent=1))


def load_state(rid):
    try:
        return json.loads(state_path(rid).read_text())
    except Exception:
        return {}


def mask_key(k):
    if not k:
        return ""
    return k[:3] + "***" + k[-4:] if len(k) > 8 else "***"


def public_state(rid):
    st = _RUNS.get(rid)
    if st is None:
        st = load_state(rid)
        if not st:
            return None
    out = {k: v for k, v in st.items() if k not in ("proc", "log_fh")}
    if "params" in out and "api_key" in out["params"]:
        out["params"] = dict(out["params"])
        out["params"]["api_key"] = mask_key(out["params"]["api_key"])
    return out


# ----------------------------------------------------------------------------
# 报告解析
# ----------------------------------------------------------------------------
def _series_for(metric, model=None):
    """返回 (匹配的 series 列表)；按 model 过滤，若无匹配则回退全部。"""
    series = metric.get("series", []) if isinstance(metric, dict) else []
    if model:
        picked = [s for s in series if s.get("labels", {}).get("model_name") == model]
        if picked:
            return picked
    return series


def _counter_total(metrics, name, model=None, label_filter=None):
    m = metrics.get(name)
    if not m:
        return None
    total = 0.0
    found = False
    for s in _series_for(m, model):
        labels = s.get("labels", {})
        if label_filter and any(labels.get(k) != v for k, v in label_filter.items()):
            continue
        st = s.get("stats", {})
        if "total" in st:
            total += float(st["total"])
            found = True
    return total if found else None


def _hist_sum_count(metrics, name, model=None):
    m = metrics.get(name)
    if not m:
        return None, None
    total_sum, total_cnt = 0.0, 0.0
    found = False
    for s in _series_for(m, model):
        st = s.get("stats", {})
        if "sum" in st and "count" in st:
            total_sum += float(st["sum"])
            total_cnt += float(st["count"])
            found = True
    return (total_sum, total_cnt) if found else (None, None)


def parse_server_metrics(sm_json, model=None):
    """解析 vLLM 服务端指标 → Prefill/Decode 服务端速率与阶段耗时。"""
    metrics = sm_json.get("metrics", {})
    # 实际参与计算（未命中前缀缓存）的 prefill tokens；缺失则回退 prompt_tokens 总数
    prompt_computed = _counter_total(
        metrics, "vllm:prompt_tokens_by_source", model, {"source": "local_compute"}
    )
    if prompt_computed is None:
        prompt_computed = _counter_total(metrics, "vllm:prompt_tokens", model)
    prompt_total = _counter_total(metrics, "vllm:prompt_tokens", model)
    gen_total = _counter_total(metrics, "vllm:generation_tokens", model)
    prefill_sum, prefill_cnt = _hist_sum_count(metrics, "vllm:request_prefill_time_seconds", model)
    decode_sum, decode_cnt = _hist_sum_count(metrics, "vllm:request_decode_time_seconds", model)
    queue_sum, _ = _hist_sum_count(metrics, "vllm:request_queue_time_seconds", model)
    ttft_sum, ttft_cnt = _hist_sum_count(metrics, "vllm:time_to_first_token_seconds", model)

    out = {"server_available": bool(prefill_sum) or bool(decode_sum)}
    if prefill_sum:
        out["prefill_avg_sec"] = prefill_sum / prefill_cnt if prefill_cnt else None
        if prompt_computed:
            out["prefill_rate"] = prompt_computed / prefill_sum
    if decode_sum:
        out["decode_avg_sec"] = decode_sum / decode_cnt if decode_cnt else None
        if gen_total:
            out["decode_rate"] = gen_total / decode_sum
    out["server_tokens_prompt_total"] = prompt_total
    out["server_tokens_generation_total"] = gen_total
    out["server_window"] = (sm_json.get("summary") or {}).get(
        "endpoints_configured", []
    )
    return out


def parse_report(profile_json_path, server_json_path=None):
    """合并客户端 + 服务端报告 → 仪表盘数据（后端格式化，前端直接渲染）。"""
    prof = json.loads(Path(profile_json_path).read_text())
    model = "unknown"
    try:
        model = prof["input_config"]["models"]["items"][0]["name"]
    except Exception:
        pass

    concurrency, req_kind = None, ""
    try:
        ph = prof["input_config"]["phases"][0]
        concurrency = ph.get("concurrency")
        req_kind = ph.get("type", "")
    except Exception:
        pass

    m = prof
    ttft_avg = m.get("time_to_first_token", {}).get("avg")
    ttft_p50 = m.get("time_to_first_token", {}).get("p50")
    ttft_p95 = m.get("time_to_first_token", {}).get("p95")
    lat_avg = m.get("request_latency", {}).get("avg")
    lat_p95 = m.get("request_latency", {}).get("p95")
    err_rate = m.get("request_error_rate", {}).get("avg", 0.0)
    isl = m.get("input_sequence_length", {}).get("avg")
    osl = m.get("output_sequence_length", {}).get("avg")
    dur = m.get("benchmark_duration", {}).get("avg")
    req_count = m.get("request_count", {}).get("avg")
    per_user = m.get("output_token_throughput_per_user", {}).get("avg")

    client_prefill_est = None
    if ttft_avg and isl:
        client_prefill_est = isl / (ttft_avg / 1000.0)

    # 服务端
    srv = {}
    if server_json_path and Path(server_json_path).exists():
        try:
            srv = parse_server_metrics(json.loads(Path(server_json_path).read_text()), model)
        except Exception:
            srv = {"server_available": False}

    # —— 顶部两张大卡 ——
    sub = "tokens/s · 包含其他流量的统计窗口"
    cards = {
        "prefill": {
            "label": "Prefill (P) 服务端阶段平均速率",
            "value": fmt_smart(srv.get("prefill_rate"), 1) if srv.get("prefill_rate") else "—",
            "sub": sub if srv.get("prefill_rate") else "未采集服务端指标",
        },
        "decode": {
            "label": "Decode (D) 服务端阶段平均速率",
            "value": fmt_smart(srv.get("decode_rate"), 1) if srv.get("decode_rate") else "—",
            "sub": sub if srv.get("decode_rate") else "未采集服务端指标",
        },
    }
    # —— 三个顶部指标 ——
    top = [
        {"label": "平均 TTFT", "value": fmt_ms(ttft_avg)},
        {"label": "平均单请求耗时", "value": f"{lat_avg / 1000:.2f} s" if lat_avg else "—"},
        {
            "label": "错误率",
            "value": (
                f"{err_rate:.0f}%" if float(err_rate or 0) == int(err_rate or 0) else f"{err_rate:.2f}%"
            ),
        },
    ]
    # —— 详细指标表（与截图一致）——
    rows = [
        ("测试模型", str(model)),
        ("并发数", fmt_int(concurrency) if concurrency is not None else "—"),
        ("请求数", fmt_int(req_count) if req_count is not None else "—"),
        ("平均输入长度", f"{fmt_smart(isl, 0)} tokens" if isl else "—"),
        ("平均输出长度", f"{fmt_smart(osl, 0)} tokens" if osl else "—"),
        (
            "Prefill 阶段平均耗时（服务端）",
            f"{srv['prefill_avg_sec']:.3f} 秒" if srv.get("prefill_avg_sec") else "—",
        ),
        (
            "Decode 阶段平均耗时（服务端）",
            f"{srv['decode_avg_sec']:.3f} 秒" if srv.get("decode_avg_sec") else "—",
        ),
        ("客户端 Prefill 估算", f"{fmt_smart(client_prefill_est, 0)} tokens/s" if client_prefill_est else "—"),
        ("客户端单用户 Decode 平均", f"{fmt_dec(per_user, 2)} tokens/s" if per_user else "—"),
        ("P50 TTFT", fmt_ms(ttft_p50) if ttft_p50 is not None else "—"),
        ("P95 TTFT", fmt_ms(ttft_p95) if ttft_p95 is not None else "—"),
        ("P95 请求耗时", fmt_ms(lat_p95) if lat_p95 is not None else "—"),
        ("测试持续时间", f"{dur:.2f} 秒" if dur else "—"),
    ]

    # —— 补充指标（可折叠）——
    def g(key, field="avg"):
        v = m.get(key, {})
        return v.get(field)

    extra_rows = [
        ("TTFT min / p99", f"{fmt_int(g('time_to_first_token','min'))} / {fmt_int(g('time_to_first_token','p99'))} ms"),
        ("请求耗时 min / avg / p99 / max", f"{fmt_int(g('request_latency','min'))} / {fmt_int(g('request_latency','avg'))} / {fmt_int(g('request_latency','p99'))} / {fmt_int(g('request_latency','max'))} ms"),
        ("Inter-Token Latency avg / p50 / p99", f"{g('inter_token_latency','avg'):.2f} / {g('inter_token_latency','p50'):.2f} / {g('inter_token_latency','p99'):.2f} ms" if g("inter_token_latency") else "—"),
        ("请求吞吐", f"{g('request_throughput'):.3f} req/s" if g("request_throughput") else "—"),
        ("输出 Token 吞吐（整体）", f"{g('output_token_throughput'):.2f} tokens/s" if g("output_token_throughput") else "—"),
        ("E2E 输出 Token 吞吐", f"{g('e2e_output_token_throughput'):.2f} tokens/s" if g("e2e_output_token_throughput") else "—"),
        ("总输入 / 总输出 Tokens", f"{fmt_int(m.get('total_isl',{}).get('avg'))} / {fmt_int(m.get('total_osl',{}).get('avg'))}"),
        ("完成请求数", fmt_int(g("completed_request_count"))),
        ("Benchmark 起止", f"{prof.get('start_time','')} → {prof.get('end_time','')}"),
    ]

    return {
        "model": model,
        "benchmark_id": prof.get("benchmark_id"),
        "aiperf_version": prof.get("aiperf_version"),
        "cards": cards,
        "top": top,
        "rows": [{"label": k, "value": v} for k, v in rows],
        "extra_rows": [{"label": k, "value": v} for k, v in extra_rows],
        "server_metrics_enabled": bool(srv.get("server_available", False)),
        "artifacts_dir": str(Path(profile_json_path).parent),
        "parsed_at": now_str(),
    }


def find_report(artifacts_root):
    """在 artifacts 目录下查找 profile_export_aiperf.json（取最新）。"""
    root = Path(artifacts_root)
    if not root.exists():
        return None, None
    cands = sorted(
        root.rglob("profile_export_aiperf.json"), key=lambda p: p.stat().st_mtime
    )
    if not cands:
        return None, None
    pj = cands[-1]
    sj = pj.parent / "server_metrics_export.json"
    return pj, (sj if sj.exists() else None)


# ----------------------------------------------------------------------------
# 运行管理
# ----------------------------------------------------------------------------
def build_cmd(p, artifact_dir):
    cmd = [
        *AIPERF_CMD, "profile",
        "--model", str(p["model"]),
        "--url", str(p["url"]),
        "--endpoint-type", str(p.get("endpoint_type", "chat")),
        "--tokenizer", str(p["tokenizer"]),
        "--synthetic-input-tokens-mean", str(p["isl_mean"]),
        "--synthetic-input-tokens-stddev", str(p["isl_stddev"]),
        "--output-tokens-mean", str(p["osl_mean"]),
        "--output-tokens-stddev", str(p["osl_stddev"]),
        "--concurrency", str(p["concurrency"]),
        "--request-count", str(p["request_count"]),
        "--output-artifact-dir", str(artifact_dir),
    ]
    corpus = str(p.get("prompt_corpus", "")).strip().lower()
    if corpus in ("sonnet", "coding", "random"):
        cmd += ["--prompt-corpus", corpus]
    if p.get("api_key"):
        cmd += ["--api-key", str(p["api_key"])]
    if p.get("streaming", True):
        cmd.append("--streaming")
    if p.get("use_server_token_count", True):
        cmd.append("--use-server-token-count")
    for ei in p.get("extra_inputs", []):
        if ei.strip():
            cmd += ["--extra-inputs", ei.strip()]
    if p.get("server_metrics"):
        cmd += ["--server-metrics", str(p["server_metrics"])]
    return cmd


def finalize_run(rid, exit_code=None, note=None):
    st = _RUNS.get(rid)
    if not st:
        return
    adir = run_dir(rid) / "artifacts"
    pj, sj = find_report(adir)
    if pj:
        try:
            result = parse_report(pj, sj)
            (run_dir(rid) / "result.json").write_text(
                json.dumps(result, ensure_ascii=False, indent=1)
            )
            st["result"] = result
            st["status"] = note or ("completed" if exit_code == 0 else "completed_with_error")
        except Exception as e:
            st["status"] = "failed"
            st["error"] = f"报告解析失败: {e}"
    else:
        st["status"] = "failed"
        st["error"] = "未生成报告（查看日志定位原因）" + (f"：{note}" if note else "")
    st["finished_at"] = now_str()
    if exit_code is not None:
        st["exit_code"] = exit_code
    fh = st.pop("log_fh", None)
    if fh:
        try:
            fh.close()
        except Exception:
            pass
    save_state(rid)


def watcher(rid):
    st = _RUNS[rid]
    proc = st["proc"]
    code = proc.wait()
    if st.get("cancel_requested"):
        finalize_run(rid, exit_code=code, note="cancelled")
    else:
        finalize_run(rid, exit_code=code)


def validate_params(p):
    need = ["model", "url", "tokenizer", "isl_mean", "osl_mean", "concurrency", "request_count"]
    for k in need:
        if p.get(k) in (None, ""):
            return f"缺少必填参数: {k}"
    try:
        if int(p["concurrency"]) <= 0 or int(p["request_count"]) <= 0:
            return "并发数与请求数必须大于 0"
    except (TypeError, ValueError):
        return "并发数/请求数必须为整数"
    return None


def fetch_models(base_url, api_key="", timeout=10):
    """通过 OpenAI 兼容接口 GET /v1/models 获取模型列表（后端代理，规避浏览器跨域）。"""
    base = str(base_url).strip().rstrip("/")
    murl = base + "/models" if base.endswith("/v1") else base + "/v1/models"
    headers = {"Accept": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    req = urllib.request.Request(murl, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8", errors="replace"))
    items = data.get("data") if isinstance(data, dict) else data
    ids = []
    if isinstance(items, list):
        for it in items:
            mid = it.get("id") if isinstance(it, dict) else str(it)
            if mid:
                ids.append(str(mid))
    return sorted(set(ids))


def start_run(params):
    err = validate_params(params)
    if err:
        return None, err
    rid = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
    rdir = run_dir(rid)
    (rdir / "artifacts").mkdir(parents=True, exist_ok=True)
    cmd = build_cmd(params, rdir / "artifacts")
    st = {
        "id": rid,
        "status": "running",
        "params": params,
        "cmd": " ".join(cmd),
        "started_at": now_str(),
        "pid": None,
    }
    with _LOCK:
        _RUNS[rid] = st
    log_fh = open(rdir / "run.log", "ab", buffering=0)
    log_fh.write(f"$ {st['cmd']}\n".encode())
    try:
        popen_kw = dict(stdout=log_fh, stderr=subprocess.STDOUT,
                        stdin=subprocess.DEVNULL, cwd=str(rdir))
        if IS_WIN:
            popen_kw["creationflags"] = _WIN_FLAGS
        else:
            popen_kw["start_new_session"] = True
        env = dict(os.environ)
        env.setdefault("PYTHONUTF8", "1")
        env.setdefault("PYTHONIOENCODING", "utf-8")
        env.setdefault("COLUMNS", "160")
        proc = subprocess.Popen(cmd, env=env, **popen_kw)
    except Exception as e:
        st["status"] = "failed"
        st["error"] = f"启动 aiperf 失败: {e}"
        st["finished_at"] = now_str()
        log_fh.close()
        save_state(rid)
        return st, None
    st["proc"] = proc
    st["pid"] = proc.pid
    st["log_fh"] = log_fh
    save_state(rid)
    threading.Thread(target=watcher, args=(rid,), daemon=True).start()
    return st, None


def cancel_run(rid):
    st = _RUNS.get(rid)
    if not st or st["status"] != "running":
        return False, "运行不存在或已结束"
    st["cancel_requested"] = True
    pid = st["proc"].pid
    kill_tree(pid, graceful=True)  # POSIX: SIGINT 优雅退出；Windows: 直接结束进程树
    if not IS_WIN:
        def hard_kill():
            time.sleep(20)
            if st["status"] == "running" and st.get("cancel_requested"):
                kill_tree(pid, graceful=False)
        threading.Thread(target=hard_kill, daemon=True).start()
    save_state(rid)
    return True, None


def tail_log(rid, nbytes=16000):
    f = run_dir(rid) / "run.log"
    if not f.exists():
        return ""
    size = f.stat().st_size
    with f.open("rb") as fh:
        if size > nbytes:
            fh.seek(size - nbytes)
        return fh.read().decode("utf-8", errors="replace")


def scan_startup():
    """服务启动时：加载历史运行；把上一进程遗留的 running 标记为 interrupted。"""
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    for d in sorted(RUNS_DIR.iterdir()):
        sp = d / "state.json"
        if not sp.exists():
            continue
        try:
            data = json.loads(sp.read_text())
        except Exception:
            continue
        rid = data.get("id") or d.name
        if data.get("status") == "running":
            data["status"] = "interrupted"
            data["error"] = "服务重启导致运行中断"
            sp.write_text(json.dumps(data, ensure_ascii=False, indent=1))
        res = d / "result.json"
        if res.exists() and "result" not in data:
            try:
                data["result"] = json.loads(res.read_text())
            except Exception:
                pass
        with _LOCK:
            _RUNS[rid] = data


# ----------------------------------------------------------------------------
# HTTP 服务
# ----------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    server_version = "AIPerfWeb/1.0"

    def log_message(self, fmt, *args):
        print(f"[{now_str()}] {self.address_string()} {fmt % args}")

    # ---- helpers ----
    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _file(self, path, ctype):
        try:
            body = Path(path).read_bytes()
        except OSError:
            self._json({"error": "not found"}, 404)
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:
            return {}

    # ---- routes ----
    def do_GET(self):
        path = self.path.split("?")[0]
        if path in ("/", "/index.html"):
            return self._file(STATIC_DIR / "index.html", "text/html; charset=utf-8")
        if path == "/api/health":
            ok = Path(AIPERF_CMD[0]).exists()
            return self._json({
                "status": "ok" if ok else "aiperf-missing",
                "aiperf_bin": "内置引擎" if DESKTOP else AIPERF_BIN,
                "default_tokenizer": DEFAULT_TOKENIZER,
                "desktop": DESKTOP,
                "data_dir": str(BASE),
                "time": now_str(),
            })
        if path == "/api/runs":
            items = []
            for rid in _RUNS:
                s = public_state(rid)
                if s:
                    items.append(_summary(s))
            # 按开始时间倒序（最新的在最前）；import-* 前缀的 id 字符串排序会错位，故按时间字段排
            items.sort(key=lambda x: x.get("started_at") or "", reverse=True)
            return self._json({"runs": items})
        m = re.match(r"^/api/runs/([^/]+)/log$", path)
        if m:
            txt = tail_log(m.group(1))
            body = txt.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            return self.wfile.write(body)
        m = re.match(r"^/api/runs/([^/]+)$", path)
        if m:
            s = public_state(m.group(1))
            if not s:
                return self._json({"error": "not found"}, 404)
            s["log_tail"] = tail_log(m.group(1))
            s["summary"] = _summary(s)
            return self._json(s)
        return self._json({"error": "not found"}, 404)

    def do_POST(self):
        path = self.path.split("?")[0]
        if path == "/api/run":
            body = self._body()
            extra = [
                s.strip()
                for s in re.split(r"[\n,]+", str(body.get("extra_inputs", "")))
                if s.strip()
            ]
            params = {
                "model": str(body.get("model", "")).strip(),
                "url": str(body.get("url", "")).strip(),
                "api_key": str(body.get("api_key", "")).strip(),
                "endpoint_type": str(body.get("endpoint_type", "chat")).strip(),
                "tokenizer": str(body.get("tokenizer", "")).strip(),
                "streaming": bool(body.get("streaming", True)),
                "use_server_token_count": bool(body.get("use_server_token_count", True)),
                "isl_mean": body.get("isl_mean"),
                "isl_stddev": body.get("isl_stddev", 0),
                "osl_mean": body.get("osl_mean"),
                "osl_stddev": body.get("osl_stddev", 0),
                "extra_inputs": extra,
                "concurrency": body.get("concurrency"),
                "request_count": body.get("request_count"),
                "server_metrics": str(body.get("server_metrics", "")).strip(),
                "prompt_corpus": str(body.get("prompt_corpus", "sonnet")).strip(),
            }
            st, err = start_run(params)
            if err:
                return self._json({"error": err}, 400)
            return self._json({"id": st["id"], "status": st["status"]})
        if path == "/api/models":
            body = self._body()
            url = str(body.get("url", "")).strip()
            if not url:
                return self._json({"error": "请先填写服务地址 (base URL)"}, 400)
            try:
                models = fetch_models(url, str(body.get("api_key", "")).strip())
            except Exception as e:
                return self._json({"error": f"获取模型列表失败: {e}"}, 502)
            return self._json({"models": models})
        m = re.match(r"^/api/runs/([^/]+)/cancel$", path)
        if m:
            ok, err = cancel_run(m.group(1))
            return self._json({"ok": ok, "error": err}, 200 if ok else 400)
        if path == "/api/import":
            body = self._body()
            src = str(body.get("path", "")).strip().rstrip("/\\")
            if DESKTOP:
                # 桌面版只监听 127.0.0.1，允许导入本机任意报告目录
                if not src or not Path(src).is_dir():
                    return self._json({"error": "目录不存在"}, 400)
            else:
                allowed = ["/root/artifacts", "/root/aiperf-web", str(RUNS_DIR)]
                if not src or not any(src == a or src.startswith(a + "/") for a in allowed):
                    return self._json({"error": f"路径不允许，仅支持 {allowed} 下目录"}, 400)
            pj, sj = find_report(src)
            if not pj:
                return self._json({"error": "目录中未找到 profile_export_aiperf.json"}, 404)
            try:
                result = parse_report(pj, sj)
            except Exception as e:
                return self._json({"error": f"解析失败: {e}"}, 500)
            rid = "import-" + time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:4]
            st = {
                "id": rid,
                "status": "imported",
                "params": {"model": result["model"], "import_from": src},
                "started_at": now_str(),
                "finished_at": now_str(),
                "result": result,
                "artifacts_dir": str(Path(pj).parent),
            }
            with _LOCK:
                _RUNS[rid] = st
            save_state(rid)
            (run_dir(rid) / "result.json").write_text(
                json.dumps(result, ensure_ascii=False, indent=1)
            )
            return self._json({"id": rid, "result": result})
        return self._json({"error": "not found"}, 404)


def _summary(s):
    r = s.get("result") or {}
    p = s.get("params") or {}
    card_p = ((r.get("cards") or {}).get("prefill") or {}).get("value", "—")
    card_d = ((r.get("cards") or {}).get("decode") or {}).get("value", "—")
    ttft = "—"
    for t in r.get("top", []):
        if t["label"] == "平均 TTFT":
            ttft = t["value"]
    return {
        "id": s["id"],
        "status": s.get("status"),
        "model": p.get("model") or (r.get("model") if r else "—"),
        "concurrency": p.get("concurrency"),
        "request_count": p.get("request_count"),
        "started_at": s.get("started_at"),
        "finished_at": s.get("finished_at"),
        "error": s.get("error"),
        "prefill_rate": card_p,
        "decode_rate": card_d,
        "ttft": ttft,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--host", default="0.0.0.0")
    args = ap.parse_args()
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    scan_startup()
    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"[{now_str()}] AIPerf Web Dashboard listening on http://{args.host}:{args.port}", flush=True)
    print(f"[{now_str()}] aiperf command: {' '.join(AIPERF_CMD)}", flush=True)

    if not IS_WIN:
        # 桌面客户端退出时发 SIGTERM：先结束仍在运行的 aiperf 进程组（它们在独立会话中，不会随父进程退出）
        def _on_term(signum, frame):
            for st in list(_RUNS.values()):
                proc = st.get("proc")
                if proc is not None and proc.poll() is None:
                    kill_tree(proc.pid, graceful=False)
            os._exit(0)
        signal.signal(signal.SIGTERM, _on_term)

    httpd.serve_forever()


if __name__ == "__main__":
    main()
