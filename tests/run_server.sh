#!/usr/bin/env bash
# 后台起服务（供验证使用）；不杀其它端口上的服务
set -e
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
PORT="${PORT:-8098}"
PY="${PY:-python3}"
setsid "$PY" -m uvicorn app.main:app --host 0.0.0.0 --port "$PORT" \
  > "/tmp/agent-core-$PORT.log" 2>&1 < /dev/null &
echo "started on $PORT, waiting..."
for i in $(seq 1 40); do
  if curl -sf "http://127.0.0.1:$PORT/api/health" >/dev/null 2>&1; then
    echo "UP after ${i}s"; break
  fi
  sleep 1
done
curl -s "http://127.0.0.1:$PORT/api/health"
echo
