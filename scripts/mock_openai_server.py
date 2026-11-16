"""最小 OpenAI 兼容流式服务（仅用于 CI 冒烟测试，纯标准库）。"""
import argparse
import json
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json"):
        data = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path.startswith("/v1/models"):
            return self._send(200, json.dumps({"object": "list", "data": [{"id": "mock-model", "object": "model"}]}))
        if self.path.startswith("/metrics"):
            return self._send(200, "", "text/plain; version=0.0.4")
        self._send(404, "{}")

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        req = json.loads(self.rfile.read(n) or b"{}")
        model = req.get("model", "mock-model")
        ntok = int(req.get("max_completion_tokens") or req.get("max_tokens") or 16)
        if not req.get("stream"):
            text = " hello" * ntok
            body = {"id": "x", "object": "chat.completion", "created": int(time.time()), "model": model,
                    "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "length"}],
                    "usage": {"prompt_tokens": 1, "completion_tokens": ntok, "total_tokens": ntok + 1}}
            return self._send(200, json.dumps(body))
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()

        def emit(obj):
            self.wfile.write(b"data: " + json.dumps(obj).encode() + b"\n\n")
            self.wfile.flush()

        time.sleep(0.05)  # 模拟 prefill
        for i in range(ntok):
            delta = {"content": " hello"}
            if i == 0:
                delta["role"] = "assistant"
            emit({"id": "x", "object": "chat.completion.chunk", "created": 0, "model": model,
                  "choices": [{"index": 0, "delta": delta, "finish_reason": None}]})
            time.sleep(0.005)
        emit({"id": "x", "object": "chat.completion.chunk", "created": 0, "model": model,
              "choices": [{"index": 0, "delta": {}, "finish_reason": "length"}]})
        if (req.get("stream_options") or {}).get("include_usage"):
            emit({"id": "x", "object": "chat.completion.chunk", "created": 0, "model": model, "choices": [],
                  "usage": {"prompt_tokens": 1, "completion_tokens": ntok, "total_tokens": ntok + 1}})
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, required=True)
    a = ap.parse_args()
    ThreadingHTTPServer(("127.0.0.1", a.port), H).serve_forever()
