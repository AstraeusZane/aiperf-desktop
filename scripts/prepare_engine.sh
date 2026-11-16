#!/bin/bash
# macOS (Apple Silicon) 构建期准备内置引擎（需联网；产物运行期完全离线）
#   src-tauri/resources/engine/
#     python/        便携 CPython（python-build-standalone, aarch64-apple-darwin）+ aiperf 及全部依赖
#     app.py / static/ / tokenizers/
set -euo pipefail
PY_MINOR="${1:-3.12}"
AIPERF_VERSION="${2:-0.13.0}"
export PYTHONUTF8=1 PYTHONIOENCODING=utf-8

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OUT="$ROOT/src-tauri/resources/engine"
rm -rf "$OUT"; mkdir -p "$OUT"

# ---- 1. 下载便携 Python ----
AUTH=()
[ -n "${GITHUB_TOKEN:-}" ] && AUTH=(-H "Authorization: Bearer $GITHUB_TOKEN")
URL=$(curl -sSL "${AUTH[@]}" https://api.github.com/repos/astral-sh/python-build-standalone/releases/latest \
  | /usr/bin/python3 -c "
import json, re, sys
minor = re.escape(sys.argv[1])
pat = re.compile(r'^cpython-' + minor + r'\.\d+\+\d+-aarch64-apple-darwin-install_only(_stripped)?\.tar\.gz$')
assets = [a for a in json.load(sys.stdin)['assets'] if pat.match(a['name'])]
assets.sort(key=lambda a: 0 if '_stripped' in a['name'] else 1)
print(assets[0]['browser_download_url'] if assets else '')
" "$PY_MINOR")
[ -n "$URL" ] || { echo "未找到 Python $PY_MINOR 的 macOS arm64 便携包"; exit 1; }
echo "Python 包: $URL"
TGZ="${RUNNER_TEMP:-/tmp}/python-standalone.tar.gz"
curl -sSL -o "$TGZ" "$URL"
tar -xzf "$TGZ" -C "$OUT"

PY="$OUT/python/bin/python3"
"$PY" --version

# ---- 2. 安装 aiperf ----
"$PY" -m pip install --no-cache-dir --disable-pip-version-check --prefer-binary "aiperf==$AIPERF_VERSION"
"$PY" -c "import aiperf, transformers, zmq, crick, numpy; print('import OK: aiperf', __import__('importlib.metadata').metadata.version('aiperf'), 'numpy', numpy.__version__)"
"$PY" -m aiperf profile --help > /dev/null

# ---- 3. 拷贝引擎文件 ----
cp "$ROOT/engine/app.py" "$OUT/"
cp -R "$ROOT/engine/static" "$ROOT/engine/tokenizers" "$OUT/"

# ---- 4. 文件型符号链接替换为实体文件（打包资源时不依赖符号链接保留） ----
find "$OUT" -type l | while read -r link; do
  if [ -f "$link" ]; then
    tmp="$link.__tmp"; cp -L "$link" "$tmp" && rm "$link" && mv "$tmp" "$link"
  fi
done

# ---- 5. 精简 ----
find "$OUT" -type d -name "__pycache__" -prune -exec rm -rf {} +
echo "引擎目录就绪: $(du -sh "$OUT" | cut -f1), $(find "$OUT" -type f | wc -l | tr -d ' ') 个文件"
