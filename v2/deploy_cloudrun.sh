#!/usr/bin/env bash
# Google Cloud Run 에 올린다. 한 번만 준비해 두면 그 다음부터는 이 파일만 다시 돌리면 된다.
#
#   bash v2/deploy_cloudrun.sh <프로젝트이름>
#
# 이렇게 굴러간다
#   · 평소에는 서버가 꺼져 있다가 누가 들어오면 켜진다 (첫 접속만 20~40초)
#   · 만든 파일은 서버가 아니라 구글 저장소(GCS)에 둔다. 서버가 꺼졌다 켜져도 남는다
#   · 사람이 몰리면 서버가 최대 3대까지 저절로 늘어난다
set -e

PROJECT="${1:?프로젝트 이름을 넣어 주세요: bash v2/deploy_cloudrun.sh my-project}"
REGION="${REGION:-asia-northeast3}"         # 서울
SERVICE="${SERVICE:-dance-studio}"
BUCKET="${BUCKET:-${PROJECT}-dance-runs}"
SA="dance-store"

gcloud config set project "$PROJECT" >/dev/null

echo "== 필요한 기능 켜기"
gcloud services enable run.googleapis.com cloudbuild.googleapis.com \
  artifactregistry.googleapis.com storage.googleapis.com secretmanager.googleapis.com

echo "== 파일 둘 곳 만들기 (이미 있으면 넘어간다)"
gcloud storage buckets create "gs://$BUCKET" --location="$REGION" \
  --uniform-bucket-level-access 2>/dev/null || echo "   이미 있음"

echo "== 저장소에 드나들 열쇠 만들기"
gcloud iam service-accounts create "$SA" --display-name="댄스 스튜디오 저장소" 2>/dev/null || true
SA_MAIL="$SA@$PROJECT.iam.gserviceaccount.com"
gcloud storage buckets add-iam-policy-binding "gs://$BUCKET" \
  --member="serviceAccount:$SA_MAIL" --role=roles/storage.objectAdmin >/dev/null

if ! gcloud secrets describe s3-key >/dev/null 2>&1; then
  OUT=$(gcloud storage hmac create "$SA_MAIL" --format="value(metadata.accessId,secret)")
  AKEY=$(echo "$OUT" | cut -f1); SKEY=$(echo "$OUT" | cut -f2)
  printf %s "$AKEY" | gcloud secrets create s3-key    --data-file=-
  printf %s "$SKEY" | gcloud secrets create s3-secret --data-file=-
  echo "   열쇠를 비밀 보관함에 넣었습니다"
else
  echo "   열쇠는 이미 있음"
fi

if ! gcloud secrets describe fal-key >/dev/null 2>&1; then
  echo "!! fal 열쇠가 없습니다. 아래를 한 번 실행한 뒤 다시 돌려 주세요"
  echo "   printf %s '<FAL_KEY>' | gcloud secrets create fal-key --data-file=-"
  exit 1
fi

echo "== 올리기 (처음에는 이미지를 만드느라 10분쯤 걸립니다)"
gcloud run deploy "$SERVICE" \
  --source v2 --region "$REGION" --allow-unauthenticated \
  --cpu 2 --memory 4Gi --no-cpu-throttling \
  --concurrency 4 --max-instances 3 --timeout 3600 \
  --set-env-vars "STORE=s3,S3_BUCKET=$BUCKET,S3_ENDPOINT=https://storage.googleapis.com,S3_REGION=$REGION,S3_PREFIX=runs,RUNS_DIR=/tmp/runs,HOST=0.0.0.0,KEEP_HOURS=336,DAILY_VIDEOS=2,DAILY_VIDEOS_IP=4,REDO_LIMIT=1" \
  --set-secrets "FAL_KEY=fal-key:latest,AWS_ACCESS_KEY_ID=s3-key:latest,AWS_SECRET_ACCESS_KEY=s3-secret:latest"

echo
echo "주소:"
gcloud run services describe "$SERVICE" --region "$REGION" --format="value(status.url)"
