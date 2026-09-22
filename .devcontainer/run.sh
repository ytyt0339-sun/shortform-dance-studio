#!/usr/bin/env bash
# 웹앱을 띄운다. Codespace 를 다시 시작했을 때도 이걸 한 번 실행하면 된다.
set -e
cd "$(dirname "$0")/.."

if [ -z "$FAL_KEY" ]; then
  echo "!! FAL_KEY 가 없습니다. GitHub → Settings → Codespaces → Secrets 에 넣어주세요."
fi

pkill -f "v2/app/studio.py" 2>/dev/null || true
nohup python v2/app/studio.py > /tmp/app.log 2>&1 &

for i in $(seq 1 60); do
  if curl -sf -o /dev/null "http://127.0.0.1:${PORT:-8020}/"; then
    echo "떴습니다 → 포트 ${PORT:-8020}"
    exit 0
  fi
  sleep 2
done
echo "!! 안 뜹니다. 로그:"; tail -30 /tmp/app.log
exit 1
