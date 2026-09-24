#!/usr/bin/env bash
# 화면과 API 를 Cloudflare 에 올린다. 계정을 만든 뒤 한 번만 준비하면,
# 그다음부터는 이 파일만 다시 돌리면 갱신된다.
#
#   bash deploy/cloudflare.sh
#
# 필요한 것
#   · CLOUDFLARE_API_TOKEN  (Workers 편집 + R2 편집 권한)
#   · ~/.fal_key.txt        fal 열쇠
#   · ~/.aws_key.txt        람다를 부를 때 쓸 AWS 열쇠 (첫 줄/둘째 줄)
set -e
cd "$(dirname "$0")/../worker"

BUCKET="${R2_BUCKET:-dance-runs}"
[ -n "$CLOUDFLARE_API_TOKEN" ] || { echo "!! CLOUDFLARE_API_TOKEN 이 필요합니다"; exit 1; }

echo "== 파일 둘 곳(R2) 만들기 (있으면 넘어감)"
npx wrangler r2 bucket create "$BUCKET" 2>/dev/null || echo "   이미 있음"

echo "== 숫자 세는 곳(KV) 만들기"
OUT=$(npx wrangler kv namespace create COUNTS 2>&1 || true)
echo "$OUT" | grep -o 'id = "[0-9a-f]*"' | head -1 \
  && echo "   ↑ 이 값을 wrangler.toml 의 kv_namespaces id 에 넣어주세요 (처음 한 번만)" \
  || echo "   이미 있음"

echo "== 비밀값 넣기 (화면에 안 찍힌다)"
tr -d '\r\n ' < "$HOME/.fal_key.txt"        | npx wrangler secret put FAL_KEY
sed -n '1p' "$HOME/.aws_key.txt" | tr -d '\r\n ' | npx wrangler secret put AWS_ACCESS_KEY_ID
sed -n '2p' "$HOME/.aws_key.txt" | tr -d '\r\n ' | npx wrangler secret put AWS_SECRET_ACCESS_KEY
# 임시 파일 주소에 서명할 값. 아무도 모르는 아무 글자면 된다.
openssl rand -hex 24 2>/dev/null | npx wrangler secret put LINK_SECRET \
  || date +%s%N | sha256sum | cut -c1-48 | npx wrangler secret put LINK_SECRET

echo "== 올리기 (화면 + API 한 번에)"
npx wrangler deploy --var BUILT_AT:"$(date +%s)"

echo
echo "주소는 위에 찍힌 https://…workers.dev 입니다."
