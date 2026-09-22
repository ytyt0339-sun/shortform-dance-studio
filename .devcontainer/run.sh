#!/usr/bin/env bash
# 웹앱을 띄운다. Codespace 가 켜질 때 저절로 돌고, 손으로 다시 돌려도 된다.
cd "$(dirname "$0")/.." || exit 1

# Codespaces 비밀값(FAL_KEY)은 환경변수로 바로 오지 않고 이 파일에 담겨 온다.
# postStartCommand 같은 자동 실행에서는 이 파일을 직접 읽어야 한다.
SHARED=/workspaces/.codespaces/shared/.env
if [ -f "$SHARED" ]; then
  set -a; . "$SHARED"; set +a
fi

if [ -z "$FAL_KEY" ]; then
  echo "!! FAL_KEY 가 없습니다 (GitHub > Settings > Codespaces > Secrets)"
fi

pkill -f "v2/app/studio.py" 2>/dev/null
sleep 1
nohup python v2/app/studio.py > /tmp/app.log 2>&1 &
echo "띄우는 중 (pid $!)"

# curl 이 없는 이미지라 파이썬으로 확인한다
python - <<'PY'
import socket, time, os, sys
port = int(os.environ.get("PORT", "8020"))
for _ in range(60):
    s = socket.socket()
    if s.connect_ex(("127.0.0.1", port)) == 0:
        print("떴습니다 → 포트", port); sys.exit(0)
    s.close(); time.sleep(2)
print("!! 안 뜹니다"); sys.exit(1)
PY
tail -5 /tmp/app.log 2>/dev/null
