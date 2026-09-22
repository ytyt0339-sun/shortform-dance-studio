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

# 기록은 /workspaces 에 남긴다. /tmp 는 껐다 켜면 비워진다.
LOG=/workspaces/app.log

pkill -f "v2/app/studio.py" 2>/dev/null
sleep 1

# setsid 로 쉘에서 완전히 떼어낸다. 자동 실행은 명령이 끝나면 그 쉘을 통째로
# 정리하기 때문에, 그냥 nohup 만 쓰면 앱도 같이 죽는다 (실제로 그랬다).
# 그리고 앱이 어쩌다 죽어도 3초 뒤 저절로 다시 뜨게 감싼다.
setsid nohup bash -c 'while true; do python v2/app/studio.py; echo "[$(date)] 앱이 멈춰서 다시 띄웁니다"; sleep 3; done' \
  >> "$LOG" 2>&1 < /dev/null &
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
tail -5 "$LOG" 2>/dev/null
