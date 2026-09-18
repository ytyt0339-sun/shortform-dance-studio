# ffmpeg 가 있어야 돌아간다. 그래서 정적 호스팅(Vercel, Netlify, GitHub Pages)에는
# 올릴 수 없고, 컨테이너를 돌리는 곳(Render, Railway, Fly.io, 클라우드 VM)에 올린다.
FROM python:3.11-slim

# ffmpeg: 영상 자르기·합치기·자막 굽기에 쓴다
# libgl1, libglib2.0-0: opencv 가 요구한다
# fonts-nanum: 한글 기본 글꼴 (사용자가 글꼴을 안 고를 때 쓰는 기본값)
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg libgl1 libglib2.0-0 fonts-nanum \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /srv

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ /srv/app/

# 자막용 한글 글꼴을 내려받는다 (약 150MB, 저장소에는 넣지 않는다).
# 글꼴 없이도 돌아가지만 고를 수 있는 글꼴이 크게 줄어든다.
RUN python app/get_fonts.py || echo "글꼴 내려받기 실패 - 기본 글꼴로 진행합니다"

# 자세 인식 모델도 첫 실행 때 받으므로 미리 받아 둔다
RUN python -c "import sys; sys.path.insert(0, 'app'); import reference as rf; rf.ensure_model('app')" \
    || echo "자세 모델은 첫 요청 때 받습니다"

ENV HOST=0.0.0.0
ENV PORT=8080
ENV RUNS_DIR=/data/runs
EXPOSE 8080

CMD ["python", "app/studio.py"]
