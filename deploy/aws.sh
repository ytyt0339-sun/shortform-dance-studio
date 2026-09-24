#!/usr/bin/env bash
# 무거운 일(자세 인식·ffmpeg)을 맡을 람다를 올린다. 계정을 만든 뒤 한 번만 돌리면 되고,
# 코드를 고친 뒤 다시 돌리면 갱신된다.
#
#   bash deploy/aws.sh
#
# 필요한 것
#   · ~/.aws_key.txt  첫 줄 액세스 키, 둘째 줄 비밀 키
#   · ~/.fal_key.txt  fal 열쇠
set -e

REGION="${AWS_REGION:-ap-northeast-2}"        # 서울
NAME="${LAMBDA_NAME:-dance-studio}"
ROLE="${NAME}-role"

# ── 열쇠 읽기 (화면에 찍지 않는다) ──
KEYFILE="$HOME/.aws_key.txt"
[ -f "$KEYFILE" ] || { echo "!! $KEYFILE 이 없습니다 (첫 줄 액세스 키, 둘째 줄 비밀 키)"; exit 1; }
export AWS_ACCESS_KEY_ID=$(sed -n '1p' "$KEYFILE" | tr -d '\r\n ')
export AWS_SECRET_ACCESS_KEY=$(sed -n '2p' "$KEYFILE" | tr -d '\r\n ')
export AWS_DEFAULT_REGION="$REGION"

ACCT=$(aws sts get-caller-identity --query Account --output text)
REPO="$ACCT.dkr.ecr.$REGION.amazonaws.com/$NAME"
echo "== 계정 $ACCT / 지역 $REGION"

echo "== 이미지 보관소 만들기 (있으면 넘어감)"
aws ecr describe-repositories --repository-names "$NAME" >/dev/null 2>&1 \
  || aws ecr create-repository --repository-name "$NAME" >/dev/null

echo "== 이미지 만들고 올리기 (2.5GB, 처음에는 10분쯤)"
aws ecr get-login-password | docker login --username AWS --password-stdin "${REPO%/*}"
# OneDrive 폴더에서 바로 구우면 도커가 파일을 못 읽는 일이 있다.
# 임시 폴더로 복사해 굽는다 (한글·동기화 폴더 문제를 피한다).
CTX=$(mktemp -d)
mkdir -p "$CTX/app" "$CTX/lambda"
cp v2/app/*.py "$CTX/app/"
cp -r v2/app/static_studio "$CTX/app/" 2>/dev/null || true
cp v2/lambda/* "$CTX/lambda/"
cp v2/requirements.txt v2/.dockerignore "$CTX/"
docker build -f "$CTX/lambda/Dockerfile" -t "$NAME" "$CTX"
rm -rf "$CTX"
docker tag "$NAME:latest" "$REPO:latest"
docker push "$REPO:latest"

echo "== 람다가 쓸 권한 만들기 (있으면 넘어감)"
if ! aws iam get-role --role-name "$ROLE" >/dev/null 2>&1; then
  aws iam create-role --role-name "$ROLE" --assume-role-policy-document '{
    "Version":"2012-10-17",
    "Statement":[{"Effect":"Allow","Principal":{"Service":"lambda.amazonaws.com"},
                  "Action":"sts:AssumeRole"}]}' >/dev/null
  aws iam attach-role-policy --role-name "$ROLE" \
    --policy-arn arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole
  echo "   권한이 퍼지길 기다리는 중 (10초)"; sleep 10
fi
ROLE_ARN=$(aws iam get-role --role-name "$ROLE" --query Role.Arn --output text)

# R2 는 S3 와 말이 통한다. 람다는 이 값으로 R2 에 파일을 넣고 뺀다.
ENV_VARS="STORE=s3,S3_BUCKET=${R2_BUCKET:-dance-runs},S3_ENDPOINT=${R2_ENDPOINT:?R2_ENDPOINT 를 넣어주세요},S3_REGION=auto,S3_PREFIX=runs,WORK_DIR=/tmp/jobs,FAL_KEY=$(tr -d '\r\n ' < "$HOME/.fal_key.txt"),AWS_ACCESS_KEY_ID=${R2_KEY:?R2_KEY},AWS_SECRET_ACCESS_KEY=${R2_SECRET:?R2_SECRET}"

if aws lambda get-function --function-name "$NAME" >/dev/null 2>&1; then
  echo "== 람다 갱신"
  aws lambda update-function-code --function-name "$NAME" \
    --image-uri "$REPO:latest" >/dev/null
  aws lambda wait function-updated --function-name "$NAME"
  aws lambda update-function-configuration --function-name "$NAME" \
    --timeout 900 --memory-size 3008 --ephemeral-storage Size=2048 \
    --environment "Variables={$ENV_VARS}" >/dev/null
else
  echo "== 람다 새로 만들기"
  # 메모리 3GB: 넉넉해야 빨리 끝난다 (람다는 메모리에 비례해 CPU 를 준다).
  # 시간 15분: 가장 오래 걸리는 마무리(합치기·자막)도 2분 안쪽이라 충분하다.
  # 임시 디스크 2GB: 영상 원본과 중간 파일이 /tmp 에 잠깐 쌓인다.
  aws lambda create-function --function-name "$NAME" \
    --package-type Image --code ImageUri="$REPO:latest" --role "$ROLE_ARN" \
    --timeout 900 --memory-size 3008 --ephemeral-storage Size=2048 \
    --environment "Variables={$ENV_VARS}" >/dev/null
fi
aws lambda wait function-updated --function-name "$NAME"
echo "== 끝. 람다 이름: $NAME"
