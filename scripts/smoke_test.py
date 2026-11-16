"""端到端冒烟测试：用“即将打包的”内置引擎，经 app.py 的 HTTP API 发起一次真实 aiperf 压测。

用法：<engine>/python/python.exe -I scripts/smoke_test.py <engine 目录>
验证点：内置 Python 可运行 app.py → /api/run 能拉起 aiperf → 压测完成 → 报告被解析。
日志输出到 ./smoke-logs/ 便于 CI 上传排查。
"""
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

IS_WIN = os.name == "nt"
NOWIN = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def http(method, url, body=None, timeout=10):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def kill(p):
    if p.poll() is not None:
        return
    if IS_WIN:
        subprocess.run(["taskkill", "/PID", str(p.pid), "/T", "/F"], capture_output=True)
    else:
        p.kill()


def main():
    engine = Path(sys.argv[1]).resolve()
    here = Path(__file__).resolve().parent
    py = Path(sys.executable)
    logs = Path.cwd() / "smoke-logs"
    logs.mkdir(exist_ok=True)
    home = Path(tempfile.mkdtemp(prefix="aiperf-smoke-"))

    mport, wport = free_port(), free_port()
    mock = subprocess.Popen([str(py), "-I", str(here / "mock_openai_server.py"), "--port", str(mport)],
                            creationflags=NOWIN)
    env = dict(os.environ,
               AIPERF_WEB_DESKTOP="1", AIPERF_WEB_HOME=str(home),
               AIPERF_WEB_STATIC=str(engine / "static"), AIPERF_PYTHON=str(py),
               AIPERF_DEFAULT_TOKENIZER=str(engine / "tokenizers" / "qwen3.8-27b"),
               HF_HUB_DISABLE_TELEMETRY="1", PYTHONUTF8="1", PYTHONIOENCODING="utf-8",
               # 模拟断网：外网 HTTP 全部走不可达代理，只放行本机 → 任何隐性联网都会让测试失败
               HTTP_PROXY="http://127.0.0.1:9", HTTPS_PROXY="http://127.0.0.1:9",
               http_proxy="http://127.0.0.1:9", https_proxy="http://127.0.0.1:9",
               NO_PROXY="127.0.0.1,localhost", no_proxy="127.0.0.1,localhost")
    for k in ("HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE"):
        env.pop(k, None)
    elog = open(logs / "engine.log", "wb")
    web = subprocess.Popen([str(py), str(engine / "app.py"), "--host", "127.0.0.1", "--port", str(wport)],
                           env=env, stdout=elog, stderr=subprocess.STDOUT, creationflags=NOWIN)
    base = f"http://127.0.0.1:{wport}"
    ok = False
    try:
        for _ in range(100):
            try:
                h = http("GET", base + "/api/health")
                break
            except Exception:
                time.sleep(0.3)
        else:
            raise SystemExit("引擎 /api/health 未就绪")
        print("health:", h)
        assert h["status"] == "ok", h

        models = http("POST", base + "/api/models", {"url": f"http://127.0.0.1:{mport}"})
        print("models:", models)
        assert models["models"] == ["mock-model"], models

        run = http("POST", base + "/api/run", {
            "model": "mock-model", "url": f"http://127.0.0.1:{mport}", "api_key": "",
            "endpoint_type": "chat", "tokenizer": h["default_tokenizer"],
            "streaming": True, "use_server_token_count": False,
            "isl_mean": 64, "isl_stddev": 0, "osl_mean": 16, "osl_stddev": 0,
            "extra_inputs": "", "prompt_corpus": "sonnet",
            "concurrency": 1, "request_count": 3, "server_metrics": "",
        })
        rid = run["id"]
        print("run started:", rid)
        deadline = time.time() + 900
        st = {}
        while time.time() < deadline:
            st = http("GET", f"{base}/api/runs/{rid}")
            if st["status"] != "running":
                break
            time.sleep(3)
        (logs / "run.log").write_text(st.get("log_tail", ""), encoding="utf-8")
        print("status:", st.get("status"), "exit:", st.get("exit_code"), "error:", st.get("error"))
        print("---- aiperf log tail ----")
        print(st.get("log_tail", "")[-4000:])
        res = st.get("result") or {}
        if res:
            print("top:", json.dumps(res.get("top"), ensure_ascii=False))
            for r in res.get("rows", []):
                print(f"  {r['label']}: {r['value']}")
        ok = st.get("status") == "completed" and bool(res) and res["top"][0]["value"] != "—"
    finally:
        kill(web)
        kill(mock)
        elog.close()
        art = home / "runs"
        if art.exists():
            shutil.copytree(art, logs / "runs", dirs_exist_ok=True)
    print("SMOKE TEST", "PASSED" if ok else "FAILED")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
