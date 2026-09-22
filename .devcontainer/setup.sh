#!/usr/bin/env bash
# Codespace 가 처음 만들어질 때 한 번 돈다.
# 이미지 안(/srv)에 구워둔 무거운 것들을 저장소 코드 쪽으로 이어준다.
set -e

cd "$(dirname "$0")/.."

# 한글 글꼴 335MB — 다시 받지 않고 이미지 안의 것을 가리킨다
if [ ! -e v2/app/fonts ] && [ -d /srv/app/fonts ]; then
  ln -s /srv/app/fonts v2/app/fonts
  echo "글꼴 연결: $(ls v2/app/fonts | wc -l)개"
fi

# 자세 인식 모델 9MB
if [ ! -e v2/app/pose_landmarker_full.task ] && [ -f /srv/app/pose_landmarker_full.task ]; then
  cp /srv/app/pose_landmarker_full.task v2/app/
  echo "자세 인식 모델 복사"
fi

mkdir -p /tmp/runs
echo "준비 끝. 실행: bash .devcontainer/run.sh"
