# AWS Lambda 용 이미지. 무거운 일(자세 인식, ffmpeg)만 맡는다.
#
# 만들기 (v2 폴더에서):
#   docker build -f lambda/Dockerfile -t dance-lambda .
# 내 컴퓨터에서 시험:
#   docker run --rm -p 9000:8080 dance-lambda
#   curl -d '{"op":"...","jid":"..."}' http://localhost:9000/2015-03-31/functions/function/invocations
# 3.12 를 쓰는 이유: 3.11 이미지는 아마존 리눅스 2 기반이라 너무 낡았다.
# 요즘 numpy·scipy 는 거기에 맞는 완성본이 없어서 소스부터 컴파일하려 들고,
# 컴파일러가 없어 실패한다. 3.12 이미지는 아마존 리눅스 2023 이라 그냥 깔린다.
FROM public.ecr.aws/lambda/python:3.12

# mesa-libGL / mesa-libEGL: mediapipe 가 자세 인식을 시작할 때 찾는다.
#   없으면 업로드는 되는데 채점에서 'libEGL.so.1 없음' 으로 실패한다 (겪었다)
# tar, xz: 아래 ffmpeg 정적 파일을 푸는 데 쓴다
RUN dnf install -y tar xz mesa-libGL mesa-libEGL glib2 && dnf clean all

# ffmpeg 은 아마존 리눅스 저장소에 없다. 어디서나 도는 정적 파일을 받아 쓴다.
ADD https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-amd64-static.tar.xz /tmp/ff.tar.xz
RUN tar -xJf /tmp/ff.tar.xz -C /tmp \
    && cp /tmp/ffmpeg-*-static/ffmpeg /tmp/ffmpeg-*-static/ffprobe /usr/local/bin/ \
    && rm -rf /tmp/ff.tar.xz /tmp/ffmpeg-*-static

COPY lambda/requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

COPY app/ ${LAMBDA_TASK_ROOT}/app/
COPY lambda/handler.py ${LAMBDA_TASK_ROOT}/

# 자막용 한글 글꼴(약 150MB)과 자세 인식 모델을 미리 넣어 둔다.
# 람다는 매번 빈 손으로 깨어나므로 그때 받으면 매번 느리다.
RUN python app/get_fonts.py || echo "글꼴 내려받기 실패 - 기본 글꼴로 진행합니다"
RUN python -c "import sys; sys.path.insert(0, 'app'); import reference as rf; rf.ensure_model('app')" \
    || echo "자세 모델은 첫 호출 때 받습니다"

CMD ["handler.handler"]
