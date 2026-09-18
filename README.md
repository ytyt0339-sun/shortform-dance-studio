# 숏폼 댄스 스튜디오

캐릭터 그림 한 장과 춤 영상 하나를 올리면, 그 캐릭터가 원하는 장소에서 춤추는
세로 숏폼(720×1280)이 나옵니다. 가사 자막과 엔딩 포스터까지 붙습니다.

생성 비용은 서비스가 부담합니다. 사용자 화면에는 금액이 나오지 않습니다.

## 저장소에 올리는 것

배포에 필요한 파일은 이게 전부입니다.

```
Dockerfile              # 실행 환경 (ffmpeg 포함)
requirements.txt        # 파이썬 패키지
render.yaml             # Render 배포 설정 (다른 곳에 올리면 없어도 됨)
.gitignore / .dockerignore
app/
  studio.py             # 웹 서버 (FastAPI)
  pipeline.py           # 그림·영상 생성, 자막, 포스터
  reference.py          # 춤 영상 채점과 크롭 (mediapipe)
  video.py              # 영상 생성 호출
  poster_text.py        # 포스터 글자 얹기
  get_fonts.py          # 한글 글꼴 내려받기 (빌드할 때 실행)
  static_studio/        # 화면 (index.html 한 장 + 예시 그림 2장)
```

**올리지 않는 것** — `.gitignore` 가 막아 둡니다.

| 빼는 것 | 이유 |
|---|---|
| `api_key.txt`, `.env` | fal 키. 새어 나가면 남이 우리 돈으로 영상을 만듭니다 |
| `app/studio_runs/` | 사용자가 만든 영상. 수백 MB이고 개인 자료입니다 |
| `app/fonts/` (약 150MB) | 빌드할 때 `get_fonts.py` 가 받습니다 |
| `app/pose_landmarker_full.task` (9MB) | 첫 실행 때 자동으로 받습니다 |
| `*.log`, `*.bak*`, `v2_*.py` 같은 옛 실험 파일 | 지금 웹앱과 무관합니다 |

## 어디에 올릴 수 있나

**ffmpeg 가 필요하고 작업이 8~12분씩 돌아갑니다.** 그래서 정적 호스팅
(GitHub Pages, Vercel, Netlify)에는 올라가지 않습니다. 컨테이너를 그대로
돌려주는 곳을 쓰세요.

- **Render** — `render.yaml` 이 있어 저장소만 연결하면 됩니다
- **Railway** / **Fly.io** — Dockerfile 을 알아서 씁니다
- **클라우드 VM** (EC2, GCE 등) — `docker build` 후 `docker run`

### 올릴 때 꼭 맞춰야 하는 것

1. **환경변수 `FAL_KEY`** — 생성 모델 호출에 씁니다. 대시보드에서 직접 넣고,
   저장소에는 절대 두지 않습니다.
2. **인스턴스 1대** — 진행 중인 작업 상태를 그 서버 메모리에 들고 있습니다.
   두 대로 늘리면 방금 누른 작업을 다른 서버가 모릅니다.
3. **디스크 붙이기** — `/data` 에 붙이고 `RUNS_DIR=/data/runs`. 없으면 서버가
   다시 뜰 때 사용자가 만든 영상이 전부 사라집니다.
4. **메모리 2GB 이상** — 자세 인식(mediapipe)이 먹습니다.
5. **자동 절전(sleep) 끄기** — 8~12분짜리 작업 도중에 서버가 잠들면 그 작업은
   사라집니다. 무료 플랜은 대부분 잠듭니다.

### 환경변수

| 이름 | 기본값 | 설명 |
|---|---|---|
| `FAL_KEY` | (없음) | **필수.** fal.ai 키 |
| `HOST` | `127.0.0.1` | 서버에 올릴 때는 `0.0.0.0` |
| `PORT` | `8020` | 호스팅이 정해주는 값을 그대로 |
| `RUNS_DIR` | `app/studio_runs` | 작업 폴더. 붙인 디스크 경로로 |
| `KEEP_HOURS` | `336` (14일) | 이 시간이 지난 작업 자동 삭제 |

## 내 컴퓨터에서 돌려보기

```bash
pip install -r requirements.txt
# ffmpeg 가 PATH 에 있어야 합니다
python app/get_fonts.py          # 글꼴 (한 번만)
setx FAL_KEY "..."               # 또는 ~/.fal_key.txt 에 저장
python app/studio.py             # http://127.0.0.1:8020
```

## 사용자끼리 섞이지 않나

로그인은 없습니다. 브라우저마다 쿠키(`studio_sid`)를 하나씩 주고, 작업마다
주인을 적어 둡니다. 남의 작업은 목록에도 안 보이고 주소를 알아도 열리지
않습니다. 다만 **쿠키를 지우면 본인도 자기 작업을 못 찾습니다.**

## 아직 없는 것

- 사용량 제한이 없습니다. 키 하나를 모두가 함께 쓰므로, 공개 전에 **1인당
  생성 횟수 제한**을 두는 편이 좋습니다.
- 로그인이 없어 기기를 옮기면 이어서 작업할 수 없습니다.
