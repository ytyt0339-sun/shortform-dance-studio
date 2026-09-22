# -*- coding: utf-8 -*-
"""캐릭터 댄스 숏폼 스튜디오 — 웹 서버.

사용자가 넣는 것: 캐릭터 이미지 · 배경 설명 · 레퍼런스 춤 영상 · 음악 · 포스터 문구
받아가는 것: 완성된 세로 영상

기존 server.py 와 별개로 둔다. 그쪽은 캐릭터를 잘라내 배경에 얹는 방식이라
평면적으로 보이는 문제가 있었고, 여기서는 장면 안에서 통째로 생성한다.
"""
import io
import json
import os
import shutil
import contextvars
import threading
import time
import traceback
import uuid
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, UploadFile, File
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

APP = Path(__file__).parent
# 작업 폴더. 서버에 올릴 때는 컨테이너가 다시 뜨면 사라지지 않도록
# 붙여둔 디스크 경로를 RUNS_DIR 로 넘긴다 (예: /data/runs).
RUNS = Path(os.environ.get("RUNS_DIR") or (APP / "studio_runs"))
RUNS.mkdir(parents=True, exist_ok=True)

import sys
sys.path.insert(0, str(APP))
import pipeline as pl
import reference as rf
import store
import video as vd


def load_key():
    """키 탐색 순서. 홈 폴더를 먼저 보는 이유: 프로젝트 폴더는 OneDrive 동기화 대상이라
    키 파일이 클라우드로 올라간다."""
    home = Path(os.path.expanduser("~"))
    for p in (home / ".fal_key.txt", APP / "api_key.txt"):
        if p.exists():
            return p.read_text(encoding="utf-8").strip()
    return ""


os.environ.setdefault("FAL_KEY", load_key())

app = FastAPI(title="캐릭터 댄스 스튜디오")

# 브라우저마다 하나씩 주는 세션. 로그인 대신 이걸로 작업을 가른다.
SID_COOKIE = "studio_sid"
_SID = contextvars.ContextVar("sid", default="")
_IP = contextvars.ContextVar("ip", default="")
# 이 시간이 지난 작업은 자동으로 지운다. 배포한 곳에서 KEEP_HOURS 로 바꿀 수 있다.
KEEP_HOURS = int(os.environ.get("KEEP_HOURS") or 24 * 14)
SWEEP_MIN = 30           # 청소 주기


SID_RE = __import__("re").compile(r"^[0-9a-f]{32}$")


@app.middleware("http")
async def session_mw(request, call_next):
    # 쿠키를 막아둔 브라우저(사생활 보호 모드, 편집기 내장 브라우저)에서는
    # 쿠키가 저장되지 않아 요청마다 다른 사람이 되어 버린다. 그래서 화면이
    # 들고 있는 값을 헤더나 주소(s=)로도 받는다. 셋 다 없을 때만 새로 만든다.
    given = (request.headers.get("x-studio-sid")
             or request.query_params.get("s")
             or request.cookies.get(SID_COOKIE) or "").strip()
    sid = given if SID_RE.match(given) else ""
    fresh = not sid
    if fresh:
        sid = uuid.uuid4().hex
    # 앞단에 프록시(Hugging Face, Render)가 있으면 진짜 IP 는 헤더에 들어온다
    fwd = (request.headers.get("x-forwarded-for") or "").split(",")[0].strip()
    ip = fwd or (request.client.host if request.client else "")
    token = _SID.set(sid)
    ip_token = _IP.set(ip)
    try:
        resp = await call_next(request)
    finally:
        _SID.reset(token)
        _IP.reset(ip_token)
    if fresh:
        resp.set_cookie(SID_COOKIE, sid, max_age=60 * 60 * 24 * 30,
                        httponly=True, samesite="lax")
    resp.headers["X-Studio-Sid"] = sid      # 화면이 받아서 저장해 둔다
    return resp


def sid():
    return _SID.get()


def client_ip():
    return _IP.get()


def sweep():
    """오래된 작업을 지운다. 결과물은 받아가는 것으로 끝이라 쌓아둘 이유가 없다."""
    cut = time.time() - KEEP_HOURS * 3600
    for d in list(RUNS.iterdir()):
        if d.name == "usage" or not d.is_dir():
            continue                      # 사용량 기록은 따로 치운다
        try:
            f = d / "job.json"
            if not f.exists() or f.stat().st_mtime < cut:
                shutil.rmtree(d, ignore_errors=True)
                store.drop(d.name + "/")        # 저장소 쪽도 함께 비운다
        except Exception:
            pass


def _sweeper():
    while True:
        time.sleep(SWEEP_MIN * 60)
        try:
            sweep()
            usage_sweep()
        except Exception:
            pass


threading.Thread(target=_sweeper, daemon=True).start()

STEPS = ["캐릭터", "배경", "키컷 확인", "레퍼런스", "영상 생성", "엔딩", "완성"]

# 이 아래로 떨어지면 결과가 무너질 확률이 높다. 돈을 쓰기 전에 확실히 알린다.
MIN_SCORE = 70
WARN_SCORE = 85


def score_check(job):
    """레퍼런스 적합도 판정. level: ok / warn / block."""
    sc = job.get("clip_score")
    if not sc:
        return None
    score = sc.get("score", 0)
    fails = list(sc.get("fail") or [])
    sub = sc.get("sub") or {}
    reasons = list(fails)
    # 항목별로 왜 낮은지 짚어준다
    tips = {
        "전신노출": "머리부터 발끝까지 다 나오는 구간이 부족합니다. 다리가 잘리면 캐릭터 다리도 뭉개집니다.",
        "단독인물": "다른 사람이 함께 잡힙니다.",
        "검출안정성": "동작이 흐릿하거나 빨라서 사람을 놓치는 구간이 많습니다.",
        "화면내유지": "인물이 화면 밖으로 자주 벗어납니다.",
        "동작크기": "동작이 너무 작아서 캐릭터가 거의 안 움직일 수 있습니다.",
        "컷전환없음": "중간에 컷이 바뀝니다. 한 번에 찍은 영상이어야 합니다.",
    }
    for k, v in sub.items():
        if k in tips and v < 0.6 and tips[k] not in reasons:
            reasons.append(tips[k])
    if not sc.get("ok") or score < MIN_SCORE:
        level = "block"
    elif score < WARN_SCORE:
        level = "warn"
    else:
        level = "ok"
    return {"level": level, "score": score, "reasons": reasons,
            "min": MIN_SCORE, "warn": WARN_SCORE}


# ---- 사용량 제한 -------------------------------------------------------------
# 키 하나로 여러 사람이 쓰므로, 한 사람이 계속 만들면 그대로 비용이 된다.
# 세션(브라우저) 단위로 하루 몇 편까지만 만들 수 있게 막는다.
DAILY_VIDEOS = int(os.environ.get("DAILY_VIDEOS") or 2)   # 하루에 만들 수 있는 편수
# 쿠키는 지우면 초기화되므로 접속한 곳(IP) 기준으로도 센다.
# 학교나 회사처럼 여러 사람이 한 IP 를 쓰는 경우가 있어 조금 넉넉하게 둔다.
DAILY_VIDEOS_IP = int(os.environ.get("DAILY_VIDEOS_IP") or DAILY_VIDEOS * 2)
REDO_LIMIT = int(os.environ.get("REDO_LIMIT") or 1)       # 한 편을 다시 만들 수 있는 횟수
_USAGE_LOCK = threading.Lock()


def _today():
    return time.strftime("%Y-%m-%d")


def _usage_key(who):
    """사람마다 파일 하나. 한 파일에 전부 몰아넣으면 서버가 여러 대일 때
    같은 파일을 동시에 고쳐 서로의 기록을 덮어쓴다."""
    import hashlib
    h = hashlib.sha1(str(who).encode("utf-8")).hexdigest()[:16]
    return "usage/%s/%s.json" % (_today(), h)


def usage_of(who):
    """오늘 이 사람이 만든 편수. 날짜가 바뀌면 0 부터 다시 센다."""
    rel = _usage_key(who)
    p = RUNS / rel
    if not p.exists():
        store.get(rel, p)              # 다른 서버가 센 것이 있을 수 있다
    try:
        u = json.loads(p.read_text(encoding="utf-8"))
        return {"date": _today(), "videos": int(u.get("videos", 0))}
    except Exception:
        return {"date": _today(), "videos": 0}


def usage_bump(who, n=1):
    """만들기 시작할 때 +1, 실패하면 -1 로 되돌린다."""
    with _USAGE_LOCK:
        rel = _usage_key(who)
        p = RUNS / rel
        cur = usage_of(who)["videos"]
        u = {"date": _today(), "videos": max(0, cur + n), "who": str(who)[:40]}
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(u, ensure_ascii=False), encoding="utf-8")
        tmp.replace(p)
        store.put(p, rel)
        return u


def usage_sweep():
    """어제까지의 사용량 기록을 지운다. 매일 한 번이면 충분하다."""
    root = RUNS / "usage"
    if not root.is_dir():
        return
    for d in list(root.iterdir()):
        if d.is_dir() and d.name != _today():
            shutil.rmtree(d, ignore_errors=True)
            store.drop("usage/%s/" % d.name)


def quota_left():
    """세션과 IP 중 더 빡빡한 쪽이 남은 편수다."""
    by_sid = DAILY_VIDEOS - usage_of(sid())["videos"]
    ip = client_ip()
    by_ip = DAILY_VIDEOS_IP - usage_of("ip:" + ip)["videos"] if ip else DAILY_VIDEOS
    return max(0, min(by_sid, by_ip))


@app.get("/api/quota")
def quota():
    u = usage_of(sid())
    return {"made": u["videos"], "limit": DAILY_VIDEOS,
            "left": quota_left(), "redo": REDO_LIMIT}


def job_dir(jid):
    """작업 폴더. 남의 작업은 없는 것처럼 취급한다.

    저장소를 쓰는 중이면 이 서버에 파일이 없을 수 있다 (다른 서버가 만들었거나
    이 서버가 방금 떴거나). 그럴 때는 작업 기록부터 끌어와서 채운다.
    """
    d = RUNS / jid
    f = d / "job.json"
    if not f.exists():
        store.get("%s/job.json" % jid, f)
    if not d.exists() or not f.exists():
        raise HTTPException(404, "작업을 찾을 수 없습니다.")
    try:
        owner = json.loads(f.read_text(encoding="utf-8")).get("owner")
    except Exception:
        owner = None
    # 주인이 비어 있는 작업(세션 분리를 넣기 전에 만든 것)은 아무에게도 안 보인다.
    # 예전에는 '주인이 없으면 누구나' 였는데, 공개 주소로 열면 남이 열 수 있다.
    if owner != sid():
        raise HTTPException(404, "작업을 찾을 수 없습니다.")
    return d


# 카메라 종류를 늘리면서 이름이 바뀌었다. 옛 작업이 깨지지 않게 옮겨준다.
CAMERA_ALIAS = {"subtle": "push", "normal": "sway", "strong": "sway_strong"}


def read_job(jid):
    job = json.loads((job_dir(jid) / "job.json").read_text(encoding="utf-8"))
    # 새 항목을 추가해도 옛 작업이 깨지지 않게 빠진 값만 채운다
    for k, v in blank_job().items():
        job.setdefault(k, v)
    # 옛 작업은 구간 정보가 없다. 플레이트에서 한 번만 읽어 채우고 저장한다.
    if job.get("plate") and not job.get("clip_dur"):
        pp = job_dir(jid) / job["plate"]
        if pp.exists():
            try:
                job["clip_dur"] = round(pl._dur(pp), 1)
                job["segments"] = max(1, int(job["clip_dur"] // pl.CUT_SEC))
                (job_dir(jid) / "job.json").write_text(
                    json.dumps(job, ensure_ascii=False, indent=2), encoding="utf-8")
            except Exception:
                pass
    job["poster_image"] = None      # 직접 올린 포스터 기능은 제거됨
    cam = job.get("camera")
    if cam in CAMERA_ALIAS:
        job["camera"] = CAMERA_ALIAS[cam]
    elif cam not in pl.CAMERA:
        job["camera"] = "sway"
    return job


_WRITE_LOCK = threading.Lock()


def write_job(jid, job):
    """같은 작업을 두 곳에서 동시에 쓰면 한쪽이 통째로 날아간다. 순서를 강제한다."""
    with _WRITE_LOCK:
        p = job_dir(jid) / "job.json"
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(job, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(p)
        store.put(p, "%s/job.json" % jid)
    # 작업 기록이 바뀌었다는 건 보통 파일이 하나 늘었다는 뜻이다 (올린 그림,
    # 만든 장면 …). 이때 같이 올려둔다. 안 그러면 서버가 죽었을 때 사라진다.
    if store.enabled():
        try:
            sync_up(jid, p.parent)
        except Exception as e:
            print("저장소 동기화 실패:", str(e)[:80])


# ---- 긴 작업 -----------------------------------------------------------------
# 키컷 생성은 20초, 영상 생성은 6분씩 걸린다. HTTP 요청 하나로 붙잡고 있으면
# 연결이 끊기는 순간 실패하므로 스레드로 돌리고 진행률만 폴링한다.
# 진행 상황은 작업 폴더의 task.json 에 적는다.
# 메모리에만 두면 서버가 다시 뜰 때 "어디까지 갔는지"가 통째로 사라지고,
# 서버를 두 대로 늘리면 한쪽이 만든 진행 상황을 다른 쪽이 모른다.
LOCK = threading.Lock()
_TASK_CACHE = {}          # jid -> (파일 수정시각, 내용) — 매번 읽지 않으려고
STALE_MIN = 25            # 이 시간이 지나도 안 끝난 '진행 중'은 죽은 것으로 본다


def _task_path(jid):
    return RUNS / jid / "task.json"


def task_get(jid):
    p = _task_path(jid)
    if not p.exists():
        store.get("%s/task.json" % jid, p)
    try:
        mt = p.stat().st_mtime
    except OSError:
        return {"state": "idle"}
    hit = _TASK_CACHE.get(jid)
    if hit and hit[0] == mt:
        return dict(hit[1])
    try:
        t = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {"state": "idle"}
    _TASK_CACHE[jid] = (mt, t)
    return dict(t)


def task_set(jid, **kw):
    with LOCK:
        t = task_get(jid)
        if t.get("state") == "idle":
            t = {}
        t.update(kw)
        p = _task_path(jid)
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp = p.with_suffix(".tmp")
            tmp.write_text(json.dumps(t, ensure_ascii=False), encoding="utf-8")
            tmp.replace(p)
            _TASK_CACHE[jid] = (p.stat().st_mtime, t)
            store.put(p, "%s/task.json" % jid)
        except OSError:
            pass                      # 폴더가 지워졌으면 그냥 넘어간다
        return dict(t)


def task_busy(jid):
    """지금 뭔가 돌고 있는가. 죽은 채 남은 기록은 아니라고 본다."""
    t = task_get(jid)
    if t.get("state") not in ("running", "waiting"):
        return False
    started = float(t.get("started") or 0)
    return (time.time() - started) < STALE_MIN * 60


def sync_up(jid, d):
    """작업 폴더에서 새로 생기거나 바뀐 파일을 저장소에 올린다.

    파일을 만드는 자리마다 올리는 코드를 넣으면 빠뜨리기 쉽다. 일이 끝날 때
    폴더를 한 번 훑어서 달라진 것만 올린다. (local 모드에서는 아무 일도 안 한다)
    """
    if not store.enabled():
        return 0
    mark = d / ".synced.json"
    try:
        seen = json.loads(mark.read_text(encoding="utf-8"))
    except Exception:
        seen = {}
    n = 0
    for f in sorted(d.rglob("*")):
        if not f.is_file() or f.name in (".synced.json", "task.json", "job.json"):
            continue
        rel = str(f.relative_to(d)).replace("\\", "/")
        st = f.stat()
        tag = "%d:%d" % (st.st_mtime_ns, st.st_size)
        if seen.get(rel) == tag:
            continue
        try:
            store.put(f, "%s/%s" % (jid, rel))
            seen[rel] = tag
            n += 1
        except Exception as e:
            print("저장소 올리기 실패:", rel, str(e)[:80])
    try:
        mark.write_text(json.dumps(seen, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass
    return n


def run_task(jid, label, fn):
    if task_busy(jid):
        raise HTTPException(409, "이미 다른 작업이 진행 중입니다.")

    owner = sid()      # 요청을 보낸 사람

    def worker():
        # 작업 스레드에는 쿠키가 없어 세션이 비어 있다. 그대로 두면 job_dir 의
        # 주인 확인에 걸려 자기 작업인데도 404 가 난다.
        _SID.set(owner)
        try:
            fn()
            # fal 에 맡기기만 한 일은 아직 안 끝났다 (state="waiting").
            # 그런 경우까지 완료로 찍지 않는다.
            sync_up(jid, RUNS / jid)
            if task_get(jid).get("state") == "running":
                task_set(jid, state="done", msg=None, at=time.time())
        except Exception as e:
            traceback.print_exc()
            task_set(jid, state="error", msg=friendly(e), at=time.time())

    task_set(jid, state="running", label=label, msg=None, done=0, total=0, started=time.time())
    threading.Thread(target=worker, daemon=True).start()
    return task_get(jid)


def friendly(e):
    s = str(e)
    if "upper body" in s:
        return ("생성 모델이 키컷에서 캐릭터 몸통을 못 찾았습니다. "
                "3단계로 돌아가 '다시 만들기'를 눌러 캐릭터가 더 크게 나온 장면을 만들어 주세요.")
    if "content_policy" in s:
        return "생성 모델이 이미지를 거부했습니다. 다른 캐릭터나 배경으로 시도해보세요."
    if "FAL_KEY" in s:
        return "API 키가 설정되지 않았습니다."
    if "Insufficient balance" in s or "402" in s:
        return "fal 잔액이 부족합니다."
    if "getaddrinfo" in s or "ConnectError" in s or "Connection" in s:
        return "생성 서버에 연결하지 못했습니다. 인터넷 연결을 확인하고 다시 눌러주세요. (자동으로 3번 재시도했습니다)"
    if "Timeout" in s or "timed out" in s:
        return "응답이 너무 오래 걸려 중단됐습니다. 다시 시도해주세요."
    return s[:300]


# ---- 작업 --------------------------------------------------------------------

def blank_job():
    """작업 기본값. 항목을 추가하면 옛 작업도 read_job 에서 이 값으로 채워진다."""
    return {
        "id": "", "created": "", "owner": "", "title": "새 영상",
        "character": None, "character_raw": None, "character_n": 0,
        "char_feedback": "", "bg_feedback": "",
        "bg_prompt": "", "bg_photo": None,
        "style_mode": "3d",
        "keycut": None, "keycut_n": 0,
        "keycut_approved": False,
        "reference": None, "plate": None, "clip_score": None, "segments": 0,
        "crop": None, "clip_start": 0.0, "clip_dur": 0.0, "seg_recs": None,
        "music": None, "music_src": "reference", "poster": ["", "", ""],
        "one_shot": True, "ref_audio": None,
        "subs": [], "subs_on": True,
        "subs_pos": "top", "subs_style": "soft", "subs_font": "", "subs_size": 0,
        "subs_color": "#FFFFFF", "subs_outline_color": "#181818",
        "subs_outline": None, "subs_bold": None,
        "camera": "sway",
        "camera_in_gen": False,
        "ending_kind": "wall", "ending_pose": "point", "ending_scene": "", "ending_sec": 4,
        "ending_prompt": "", "ending_photo": None, "ending_still": None,
        "ending_still_n": 0,
        "poster_accent": "#E8B22E", "poster_bg": "#FCFAF5", "poster_ink": "#26221C",
        "poster_font": "", "poster_bar": True, "poster_image": None,
        "poster_mode": "bake", "ending_baked": None,
        "cuts": [], "ending_raw": None, "ending": None, "result": None,
        "spent": 0.0, "render_n": 0,
        # fal 에 맡겨둔 일. {"stage","reqs":[{"id","out","done"}],"engine","want_ending"}
        "pending": None,
    }


@app.post("/api/new")
def new_job():
    jid = time.strftime("%m%d-%H%M-") + uuid.uuid4().hex[:4]
    (RUNS / jid).mkdir(parents=True, exist_ok=True)
    job = blank_job()
    job["id"] = jid
    job["owner"] = sid()
    job["created"] = time.strftime("%Y-%m-%d %H:%M")
    (RUNS / jid / "job.json").write_text(json.dumps(job, ensure_ascii=False, indent=2),
                                         encoding="utf-8")
    return job


_FONTS = []


@app.get("/api/fonts")
def fonts():
    if not _FONTS:
        _FONTS.extend(pl.system_fonts())
    return {"fonts": _FONTS,
            "poster_kinds": [{"id": k, "label": v["label"]} for k, v in pl.POSTER_KINDS.items()],
            "ending_poses": [{"id": k, "label": v["label"]} for k, v in pl.ENDING_POSES.items()],
            "styles": [{"id": k, "label": v["label"]} for k, v in pl.SUB_STYLES.items()],
            "cameras": [{"id": k, "label": v["label"]} for k, v in pl.CAMERA.items()],
            "style_modes": [{"id": k, "label": v["label"], "note": v["note"]}
                            for k, v in pl.STYLE_MODES.items()]}


@app.get("/api/version")
def version():
    """화면이 최신인지 확인용. /api/{jid} 보다 먼저 등록해야 한다."""
    f = APP / "static_studio" / "index.html"
    return {"page_mtime": int(f.stat().st_mtime) if f.exists() else 0}


@app.get("/api/fontcss")
def font_css():
    """번들 글꼴을 브라우저에서도 미리 볼 수 있게 @font-face 로 내려준다."""
    from fastapi.responses import Response
    rules = []
    for fam, fn in pl.bundled_font_map().items():
        rules.append("@font-face{font-family:'%s';src:url('/fontfile/%s') format('truetype');"
                     "font-display:swap}" % (fam.replace("'", ""), fn))
    return Response(chr(10).join(rules), media_type="text/css",
                    headers={"Cache-Control": "public, max-age=86400"})


@app.get("/fontfile/{name}")
def font_file_get(name: str):
    p = pl.BUNDLED_FONTS / Path(name).name
    if not p.exists():
        raise HTTPException(404, "글꼴 파일이 없습니다.")
    return FileResponse(p, media_type="font/ttf",
                        headers={"Cache-Control": "public, max-age=86400"})


@app.delete("/api/{jid}")
def delete_job(jid: str):
    """작업 폴더를 통째로 지운다. 만든 영상도 같이 사라진다.

    남의 작업은 job_dir 에서 이미 404 로 막힌다.
    """
    d = job_dir(jid)
    shutil.rmtree(d, ignore_errors=True)
    store.drop(jid + "/")
    _TASK_CACHE.pop(jid, None)      # 폴더째 지워지므로 task.json 도 같이 사라진다
    return {"ok": True, "id": jid}


@app.get("/api/jobs")
def list_jobs():
    """내 세션의 작업만. 남의 것은 목록에도 안 나온다."""
    me = sid()
    out = []
    for d in sorted(RUNS.iterdir(), reverse=True):
        f = d / "job.json"
        if not f.exists():
            continue
        try:
            j = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        if j.get("owner") != me:          # 주인 없는 옛 작업도 목록에서 뺀다
            continue
        out.append({"id": j["id"], "title": j.get("title"), "created": j["created"],
                    "result": bool(j.get("result")), "spent": j.get("spent", 0)})
    return out


@app.get("/api/{jid}")
def get_job(jid: str):
    job = read_job(jid)
    job["score_check"] = score_check(job)
    return job


CHECK_SEC = 5          # fal 에 물어보는 간격
_LAST_CHECK = {}


@app.get("/api/{jid}/task")
def get_task(jid: str):
    """진행 상황. 맡겨둔 일이 있으면 이 참에 fal 에도 확인한다.

    따로 도는 일꾼을 두지 않아도 되고, 서버가 재시작돼도 작업 파일에 적힌
    접수 번호로 이어서 받는다.
    """
    t = task_get(jid)
    try:
        j = read_job(jid)
    except HTTPException:
        return t
    p = j.get("pending")
    # 진행 상황 기록이 없는데 맡긴 일만 남아 있는 경우 (아주 옛 작업 등). 작업 파일에 맡긴 일이
    # 남아 있으면 '아직 하는 중'으로 되살려 준다 (화면이 끊긴 줄 알지 않도록).
    if p and t.get("state") in (None, "idle"):
        t = task_set(jid, state="waiting", label="영상 만들기",
                     msg="하던 작업을 이어받는 중", started=time.time(),
                     done=sum(1 for r in (p.get("reqs") or []) if r.get("done")),
                     total=len(p.get("reqs") or []))
    if p and p.get("stage") == "cuts":
        last = _LAST_CHECK.get(jid, 0)
        if time.time() - last >= CHECK_SEC:
            _LAST_CHECK[jid] = time.time()
            try:
                _collect_cuts(jid, RUNS / jid)
            except Exception as e:
                traceback.print_exc()
                _fail(jid, RUNS / jid, friendly(e))
        t = task_get(jid)
    return t


@app.delete("/api/{jid}")
def del_job(jid: str):
    shutil.rmtree(job_dir(jid), ignore_errors=True)
    return {"ok": True}


@app.get("/api/{jid}/file/{name}")
def get_file(jid: str, name: str):
    d = job_dir(jid)
    p = d / name
    # 저장소를 쓰는 중이면 임시 주소로 보내 저장소에서 바로 받게 한다.
    # 큰 영상을 서버가 통째로 받아서 다시 내보내면 그만큼 느리고 비싸다.
    if store.enabled():
        u = store.link("%s/%s" % (jid, name), filename=name)
        if u:
            return RedirectResponse(u, status_code=302)
    if not p.exists():
        store.get("%s/%s" % (jid, name), p)      # 다른 서버가 만든 파일일 수 있다
    if not p.exists():
        raise HTTPException(404, "파일이 없습니다.")
    # 같은 이름으로 덮어쓰는 파일이 많다(character.png, plate.mp4 …).
    # 캐시를 허용하면 새로 올려도 예전 것이 계속 보인다.
    return FileResponse(p, headers={"Cache-Control": "no-cache, must-revalidate"})


@app.get("/api/{jid}/download")
def download(jid: str):
    job = read_job(jid)
    if not job.get("result"):
        raise HTTPException(400, "아직 완성된 영상이 없습니다.")
    name = "%s.mp4" % (job.get("title") or "video")
    rel = "%s/%s" % (jid, job["result"])
    if store.enabled():                      # 저장소에서 바로 받게 한다
        u = store.link(rel, filename=name, inline=False)
        if u:
            return RedirectResponse(u, status_code=302)
    p = job_dir(jid) / job["result"]
    store.get(rel, p)
    return FileResponse(p, media_type="video/mp4", filename=name)


# ---- 1. 캐릭터 ---------------------------------------------------------------

@app.post("/api/{jid}/character")
async def set_character(jid: str, file: UploadFile = File(...)):
    d = job_dir(jid)
    ext = Path(file.filename or "c.png").suffix.lower() or ".png"
    if ext not in (".png", ".jpg", ".jpeg", ".webp"):
        raise HTTPException(400, "PNG, JPG, WEBP 이미지만 올릴 수 있습니다.")
    name = "character" + ext
    (d / name).write_bytes(await file.read())
    job = read_job(jid)
    job["character"] = name
    job["character_raw"] = None
    job["character_n"] = job.get("character_n", 0) + 1
    job["keycut"] = None
    job["keycut_approved"] = False
    write_job(jid, job)
    return job


@app.post("/api/{jid}/style")
def set_style(jid: str, style_mode: str = Form(...)):
    job = read_job(jid)
    if style_mode not in pl.STYLE_MODES:
        raise HTTPException(400, "없는 화풍입니다.")
    job["style_mode"] = style_mode
    write_job(jid, job)
    return job


@app.post("/api/{jid}/character/cleanup")
def cleanup_character(jid: str, feedback: str = Form("")):
    """낙서 그림을 캐릭터로 다듬는다. 원본은 따로 남긴다. $0.08

    feedback 은 '다시 만들 때 이건 고쳐달라'는 사용자의 문장. 원본에서 다시
    다듬으므로, 여러 번 눌러도 고친 내용이 쌓여 뭉개지지 않는다.
    """
    d = job_dir(jid)
    job = read_job(jid)
    if not job.get("character"):
        raise HTTPException(400, "먼저 그림을 올려주세요.")
    if job.get("style_mode") != "doodle":
        raise HTTPException(400, "그림 다듬기는 손그림 낙서체에서만 씁니다. "
                                 "화풍을 바꾸거나 그대로 진행해주세요.")

    def work():
        j = read_job(jid)
        src = d / (j.get("character_raw") or j["character"])
        if not j.get("character_raw"):
            raw = d / ("raw" + src.suffix)
            raw.write_bytes(src.read_bytes())
            j["character_raw"] = raw.name
            src = raw
        task_set(jid, msg="그림을 캐릭터로 다듬는 중")
        out = d / "character_clean.png"
        pl.clean_sketch(str(src), str(out), feedback=feedback)
        j["char_feedback"] = feedback.strip()
        j["character"] = out.name
        j["character_n"] = j.get("character_n", 0) + 1
        j["keycut"] = None
        j["keycut_approved"] = False
        j["spent"] = round(j.get("spent", 0) + pl.NB_PRICE, 3)
        write_job(jid, j)

    return run_task(jid, "그림 다듬기", work)


# ---- 2~3. 배경 + 키컷 ---

@app.post("/api/{jid}/bg_photo")
async def set_bg_photo(jid: str, file: UploadFile = File(None), clear: str = Form("false")):
    """배경 참고 사진(선택). 그 장소를 그대로 옮겨 그린다."""
    d = job_dir(jid)
    job = read_job(jid)
    if clear == "true" or file is None:
        if job.get("bg_photo"):
            (d / job["bg_photo"]).unlink(missing_ok=True)
        job["bg_photo"] = None
    else:
        ext = Path(file.filename or "p.jpg").suffix.lower() or ".jpg"
        if ext not in (".png", ".jpg", ".jpeg", ".webp"):
            raise HTTPException(400, "PNG, JPG, WEBP 이미지만 올릴 수 있습니다.")
        name = "bg_photo" + ext
        (d / name).write_bytes(await file.read())
        job["bg_photo"] = name
    write_job(jid, job)
    return job


@app.post("/api/{jid}/keycut")
def make_keycut(jid: str, bg_prompt: str = Form(""), feedback: str = Form("")):
    """배경 설명을 받아 '장면 안에 서 있는 캐릭터' 한 장을 만든다.

    이 한 장이 Kling 의 입력이 되고, 배경까지 그대로 영상에 실린다.
    """
    d = job_dir(jid)
    job = read_job(jid)
    if not job.get("character"):
        raise HTTPException(400, "캐릭터 이미지를 먼저 올려주세요.")
    if not bg_prompt.strip() and not job.get("bg_photo"):
        raise HTTPException(400, "배경 설명을 쓰거나 장소 사진을 올려주세요.")

    def work():
        j = read_job(jid)
        n = j.get("keycut_n", 0) + 1
        out = d / ("keycut%d.png" % n)
        task_set(jid, msg="장면 안에 캐릭터를 그리는 중")
        photo = (d / j["bg_photo"]) if j.get("bg_photo") else None
        pl.keycut(str(d / j["character"]), bg_prompt, str(out),
                  place_photo=(str(photo) if photo else None),
                  style_mode=j.get("style_mode", "3d"),
                  feedback=feedback)
        j["bg_prompt"] = bg_prompt
        j["bg_feedback"] = feedback.strip()
        j["keycut"] = out.name
        j["keycut_n"] = n
        j["keycut_approved"] = False
        j["spent"] = round(j.get("spent", 0) + pl.NB_PRICE, 3)
        if j.get("title") == "새 영상":
            j["title"] = (bg_prompt.strip() or "사진 배경")[:24]
        write_job(jid, j)

    return run_task(jid, "키컷 만들기", work)


@app.post("/api/{jid}/keycut/approve")
def approve_keycut(jid: str, approved: str = Form("true")):
    job = read_job(jid)
    if not job.get("keycut"):
        raise HTTPException(400, "키컷이 아직 없습니다.")
    job["keycut_approved"] = (approved == "true")
    write_job(jid, job)
    return job


# ---- 4. 레퍼런스 -------------------------------------------------------------

@app.post("/api/{jid}/reference")
async def set_reference(jid: str, file: UploadFile = File(...)):
    """춤 영상. 한 사람만 나와야 하고, 세로 9:16 로 자동 크롭한다."""
    d = job_dir(jid)
    ext = Path(file.filename or "r.mp4").suffix.lower() or ".mp4"
    name = "reference" + ext
    (d / name).write_bytes(await file.read())

    # 여러 명이 나오면 Kling 이 한 사람을 못 따라간다. 돈 쓰기 전에 막는다.
    try:
        chk = rf.count_people(str(d / name), str(APP))
    except Exception:
        chk = {"ok": True, "reason": None}
    if not chk.get("ok"):
        (d / name).unlink(missing_ok=True)
        return JSONResponse({"error": "%s 레퍼런스는 한 사람만 나와야 합니다." % chk.get("reason")},
                            status_code=400)

    job = read_job(jid)
    job["reference"] = name
    job["plate"] = None
    write_job(jid, job)

    def work():
        j = read_job(jid)
        task_set(jid, msg="인물을 찾아 세로로 자르는 중")
        box = rf.auto_crop(str(d / name), str(APP))
        dur = min(rf.MAX_DUR, rf.probe(d / name)["duration"])
        rf.make_plate(str(d / name), str(d / "plate.mp4"), 0.0, dur, box)
        j["crop"] = box
        j["clip_start"] = 0.0
        j["clip_dur"] = round(dur, 1)
        j["seg_recs"] = None
        task_set(jid, msg="적합도 채점 중")
        res = rf.analyze(str(d / "plate.mp4"), str(APP))
        vd.thumb_sheet(str(d / "plate.mp4"), str(d / "plate_sheet.jpg"))
        j["plate"] = "plate.mp4"
        j["clip_score"] = res
        j["segments"] = max(1, int(dur // pl.CUT_SEC))
        # 레퍼런스에 소리가 있으면 뽑아둔다. 그 영상에서 동작을 따왔으므로 박자가 맞는다.
        if pl.has_audio(d / name):
            task_set(jid, msg="레퍼런스 소리 추출 중")
            if pl.extract_audio(d / name, d / "ref_audio.mp3"):
                j["ref_audio"] = "ref_audio.mp3"
        write_job(jid, j)

    return run_task(jid, "레퍼런스 분석", work)


@app.post("/api/{jid}/reference/scan")
def scan_segments(jid: str):
    """구간별 점수를 내서 목록만 만든다. 고르는 건 사용자가 한다. 무료."""
    d = job_dir(jid)
    job = read_job(jid)
    if not job.get("reference"):
        raise HTTPException(400, "먼저 춤 영상을 올려주세요.")

    def work():
        j = read_job(jid)
        src = d / j["reference"]
        want = min(rf.MAX_DUR, rf.probe(src)["duration"])
        # 실제로 만들어질 화면(세로 크롭)에서 재야 점수가 맞는다
        task_set(jid, msg="세로로 잘라 훑는 중")
        box = j.get("crop") or rf.auto_crop(str(src), str(APP))
        j["crop"] = box
        full = min(180.0, rf.probe(src)["duration"])
        rf.make_plate(str(src), str(d / "scan.mp4"), 0.0, full, box)
        task_set(jid, msg="구간별로 점수 내는 중", done=0, total=0)
        recs = rf.recommend(str(d / "scan.mp4"), str(APP), want=want, step=3.0, top=6,
                            progress=lambda i, n: task_set(jid, done=i, total=n))
        (d / "scan.mp4").unlink(missing_ok=True)
        j["seg_recs"] = recs
        write_job(jid, j)

    return run_task(jid, "좋은 구간 찾기", work)


@app.post("/api/{jid}/reference/pick")
def pick_segment(jid: str, start: str = Form("0")):
    """추천 목록에서 고른 구간으로 플레이트를 다시 만든다. 무료."""
    d = job_dir(jid)
    job = read_job(jid)
    if not job.get("reference"):
        raise HTTPException(400, "먼저 춤 영상을 올려주세요.")
    try:
        t0 = max(0.0, float(start))
    except ValueError:
        raise HTTPException(400, "구간 시작 시각이 잘못됐습니다.")

    def work():
        j = read_job(jid)
        src = d / j["reference"]
        info = rf.probe(src)
        dur = min(rf.MAX_DUR, max(rf.MIN_DUR, info["duration"] - t0))
        box = j.get("crop") or rf.auto_crop(str(src), str(APP))
        task_set(jid, msg="고른 구간으로 다시 자르는 중")
        rf.make_plate(str(src), str(d / "plate.mp4"), t0, dur, box)
        task_set(jid, msg="적합도 채점 중")
        j["clip_score"] = rf.analyze(str(d / "plate.mp4"), str(APP))
        vd.thumb_sheet(str(d / "plate.mp4"), str(d / "plate_sheet.jpg"))
        j["crop"] = box
        j["clip_start"] = round(t0, 1)
        j["clip_dur"] = round(dur, 1)
        j["segments"] = max(1, int(dur // pl.CUT_SEC))
        write_job(jid, j)

    return run_task(jid, "구간 적용", work)


@app.post("/api/{jid}/music")
async def set_music(jid: str, file: UploadFile = File(...)):
    d = job_dir(jid)
    ext = Path(file.filename or "m.mp3").suffix.lower() or ".mp3"
    name = "music" + ext
    (d / name).write_bytes(await file.read())
    job = read_job(jid)
    job["music"] = name
    job["music_src"] = "file"
    write_job(jid, job)
    return job


@app.post("/api/{jid}/options")
def set_options(jid: str, music_src: str = Form(None), one_shot: str = Form(None),
                subs_pos: str = Form(None), subs_style: str = Form(None),
                subs_font: str = Form(None), subs_size: str = Form(None),
                subs_color: str = Form(None), subs_outline_color: str = Form(None),
                subs_outline: str = Form(None), subs_bold: str = Form(None),
                camera: str = Form(None), camera_in_gen: str = Form(None)):
    """음악 출처, 생성 방식, 자막 모양."""
    job = read_job(jid)
    if music_src in ("reference", "file", "none"):
        job["music_src"] = music_src
    if one_shot in ("true", "false"):
        job["one_shot"] = (one_shot == "true")
    if subs_pos in ("top", "bottom"):
        job["subs_pos"] = subs_pos
    if subs_style in pl.SUB_STYLES:
        job["subs_style"] = subs_style
    if subs_font is not None:
        job["subs_font"] = subs_font
    if subs_size is not None:
        try:
            job["subs_size"] = max(0.0, min(0.09, float(subs_size)))
        except ValueError:
            pass
    for k, v in (("subs_color", subs_color), ("subs_outline_color", subs_outline_color)):
        if v:
            job[k] = v
    if subs_outline is not None:
        try:
            job["subs_outline"] = max(0.0, min(0.30, float(subs_outline)))
        except ValueError:
            pass
    if subs_bold in ("true", "false"):
        job["subs_bold"] = (subs_bold == "true")
    if camera in pl.CAMERA:
        job["camera"] = camera
    if camera_in_gen in ("true", "false"):
        job["camera_in_gen"] = (camera_in_gen == "true")
    write_job(jid, job)
    return job


def audio_for(job, d):
    """실제로 깔 소리를 고른다."""
    src = job.get("music_src", "reference")
    if src == "none":
        return None
    if src == "file" and job.get("music"):
        return d / job["music"]
    if job.get("ref_audio"):
        return d / job["ref_audio"]
    return (d / job["music"]) if job.get("music") else None


@app.post("/api/{jid}/poster")
def set_poster(jid: str, l1: str = Form(""), l2: str = Form(""), l3: str = Form("")):
    job = read_job(jid)
    job["poster"] = [l1.strip(), l2.strip(), l3.strip()]
    write_job(jid, job)
    # 이미 만들어둔 엔딩이 있으면 글자만 다시 얹는다 (무료)
    d = job_dir(jid)
    if job.get("poster_mode", "bake") == "bake":
        # 심는 방식은 영상 안에 포스터가 들어가 있어서 글자만 바꿀 수 없다
        d2 = job_dir(jid)
        jj = read_job(jid)
        _bake(jj, d2)
        write_job(jid, jj)
        return jj
    if job.get("ending_raw") and (d / job["ending_raw"]).exists():
        def work():
            j = read_job(jid)
            task_set(jid, msg="포스터 글자 얹는 중")
            pl.poster_text(d / j["ending_raw"], j["poster"], d / "ending.mp4",
                           custom=poster_custom(j, d), **poster_design(j),
                           progress=lambda i, n: task_set(jid, done=i, total=n))
            j["ending"] = "ending.mp4"
            if j.get("result"):
                task_set(jid, msg="영상 다시 합치는 중", done=0, total=0)
                pl.finish([d / c for c in j["cuts"]], d / "ending.mp4",
                          audio_for(j, d), d / "final_raw.mp4")
                apply_subs(j, d)
            write_job(jid, j)
        return run_task(jid, "포스터 문구 반영", work)
    return job


@app.post("/api/{jid}/ending/options")
def ending_options(jid: str, ending_kind: str = Form(None), ending_pose: str = Form(None),
                   ending_scene: str = Form(None), ending_sec: str = Form(None),
                   ending_prompt: str = Form(None),
                   poster_accent: str = Form(None), poster_bg: str = Form(None),
                   poster_ink: str = Form(None), poster_font: str = Form(None),
                   poster_bar: str = Form(None), poster_mode: str = Form(None)):
    job = read_job(jid)
    if ending_kind in pl.POSTER_KINDS:
        job["ending_kind"] = ending_kind
    if ending_pose in pl.ENDING_POSES:
        job["ending_pose"] = ending_pose
    if ending_scene is not None:
        job["ending_scene"] = ending_scene.strip()
    if ending_prompt is not None:
        job["ending_prompt"] = ending_prompt.strip()
    if ending_sec is not None:
        try:
            job["ending_sec"] = max(4, min(8, int(float(ending_sec))))
        except ValueError:
            pass
    for k, v in (("poster_accent", poster_accent), ("poster_bg", poster_bg),
                 ("poster_ink", poster_ink)):
        if v:
            job[k] = v
    if poster_font is not None:
        job["poster_font"] = poster_font
    if poster_bar in ("true", "false"):
        job["poster_bar"] = (poster_bar == "true")
    if poster_mode in ("bake", "overlay"):
        job["poster_mode"] = poster_mode
    write_job(jid, job)
    return job


@app.get("/api/{jid}/poster_preview")
def poster_preview(jid: str):
    """지금 설정으로 만든 포스터를 그림으로 보여준다. 무료이고 즉시 만들어진다."""
    d = job_dir(jid)
    j = read_job(jid)
    out = d / "poster_art.png"
    pl.poster_image(j.get("poster") or ["", "", ""], out, **poster_design(j))
    return FileResponse(out, media_type="image/png",
                        headers={"Cache-Control": "no-store"})


def poster_design(j):
    return dict(accent=j.get("poster_accent"), bg=j.get("poster_bg"), ink=j.get("poster_ink"),
                font=pl.font_file(j.get("poster_font")), bar=j.get("poster_bar", True))


def poster_custom(j, d):
    """포스터는 앱 안에서 만든 것만 쓴다.

    직접 올린 파일을 심었더니 그 안의 인물 사진 때문에 생성 모델이 장면을
    거부했다. 만드는 쪽으로 통일하면 이 실패가 없어지고 디자인도 일관된다.
    """
    return None


@app.post("/api/{jid}/ending/photo")
async def set_ending_photo(jid: str, file: UploadFile = File(None), clear: str = Form("false")):
    """엔딩 장소 참고 사진(선택)."""
    d = job_dir(jid)
    job = read_job(jid)
    if clear == "true" or file is None:
        if job.get("ending_photo"):
            (d / job["ending_photo"]).unlink(missing_ok=True)
        job["ending_photo"] = None
    else:
        ext = Path(file.filename or "p.jpg").suffix.lower() or ".jpg"
        if ext not in (".png", ".jpg", ".jpeg", ".webp"):
            raise HTTPException(400, "PNG, JPG, WEBP 이미지만 올릴 수 있습니다.")
        name = "ending_photo" + ext
        (d / name).write_bytes(await file.read())
        job["ending_photo"] = name
    write_job(jid, job)
    return job


def _make_still(j, d, jid):
    n = j.get("ending_still_n", 0) + 1
    out = d / ("poster%d.png" % n)
    photo = (d / j["ending_photo"]) if j.get("ending_photo") else None
    pl.poster_scene(str(d / j["keycut"]), j.get("bg_prompt", ""), str(out),
                    kind=j.get("ending_kind", "wall"),
                    pose=j.get("ending_pose", "point"),
                    scene=j.get("ending_scene") or None,
                    free=j.get("ending_prompt") or None,
                    place_photo=(str(photo) if photo else None),
                    style_mode=j.get("style_mode", "3d"))
    j["ending_still"] = out.name
    j["ending_still_n"] = n
    j["spent"] = round(j.get("spent", 0) + pl.NB_PRICE, 3)
    j["ending_baked"] = None
    _bake(j, d)
    return out


def _bake(j, d):
    """포스터를 심은 장면 그림. 실패해도 장면 자체는 살려둔다."""
    if j.get("poster_mode", "bake") != "bake" or not j.get("ending_still"):
        return None
    try:
        out = d / "poster_baked.png"
        pl.bake_poster(d / j["ending_still"], j.get("poster") or ["", "", ""], out,
                       custom=poster_custom(j, d), **poster_design(j))
        j["ending_baked"] = out.name
        return out
    except Exception as e:
        j["ending_baked"] = None
        print("포스터 심기 실패:", e)
        return None


def build_ending(j, d, jid):
    """엔딩 영상을 만든다. 세 군데서 같은 것을 쓰도록 한 곳에 모았다.

    심는 방식이면 포스터를 넣은 그림으로 영상을 만들고, 그 뒤에 글자를 또 얹지 않는다.
    (얹으면 두 번 들어가고, 원본 분홍 그림을 쓰면 분홍이 그대로 남는다)
    """
    import shutil as _sh
    if not j.get("ending_still"):
        _make_still(j, d, jid)
    baked = _bake(j, d) if j.get("poster_mode", "bake") == "bake" else None
    src = baked or (d / j["ending_still"])

    task_set(jid, msg="엔딩 영상 만드는 중")
    pl.poster_video(str(src), str(d / "ending_raw.mp4"),
                    sec=j.get("ending_sec", 4), baked=bool(baked))
    j["ending_raw"] = "ending_raw.mp4"
    j["spent"] = round(j.get("spent", 0) + pl.POSTER_VID_PRICE, 3)

    if baked:
        _sh.copy(str(d / "ending_raw.mp4"), str(d / "ending.mp4"))
    else:
        task_set(jid, msg="포스터 글자 얹는 중")
        pl.poster_text(d / "ending_raw.mp4", j.get("poster") or ["", "", ""],
                       d / "ending.mp4", **poster_design(j),
                       progress=lambda i, n: task_set(jid, done=i, total=n))
    j["ending"] = "ending.mp4"
    return "ending.mp4"


@app.post("/api/{jid}/ending/still")
def make_ending_still(jid: str):
    """장면 이미지만 만든다. 마음에 들 때까지 여기서 돌린다. $0.08"""
    d = job_dir(jid)
    job = read_job(jid)
    if not job.get("keycut"):
        raise HTTPException(400, "먼저 키컷을 만들어주세요.")

    def work():
        j = read_job(jid)
        task_set(jid, msg="엔딩 장면 그리는 중")
        _make_still(j, d, jid)
        write_job(jid, j)

    return run_task(jid, "엔딩 장면 만들기", work)


@app.post("/api/{jid}/ending/video")
def make_ending_video(jid: str):
    """확인한 장면 이미지를 영상으로 만든다. $0.54"""
    d = job_dir(jid)
    job = read_job(jid)
    if not job.get("ending_still"):
        raise HTTPException(400, "먼저 엔딩 장면 이미지를 만들어주세요.")

    def work():
        j = read_job(jid)
        build_ending(j, d, jid)
        if j.get("result") and (d / "final_raw.mp4").exists():
            task_set(jid, msg="영상 다시 합치는 중", done=0, total=0)
            pl.finish([d / c for c in j["cuts"]], d / "ending.mp4",
                      audio_for(j, d), d / "final_raw.mp4")
            apply_subs(j, d)
        write_job(jid, j)

    return run_task(jid, "엔딩 영상 만들기", work)


@app.post("/api/{jid}/ending")
def make_ending(jid: str):
    """엔딩만 따로 만든다. 마음에 안 들면 여기만 다시 뽑으면 된다."""
    d = job_dir(jid)
    job = read_job(jid)
    if not job.get("keycut"):
        raise HTTPException(400, "먼저 키컷을 만들어주세요.")

    def work():
        j = read_job(jid)
        task_set(jid, msg="엔딩 장면 그리는 중")
        _make_still(j, d, jid)
        build_ending(j, d, jid)
        if j.get("result") and (d / "final_raw.mp4").exists():
            task_set(jid, msg="영상 다시 합치는 중", done=0, total=0)
            pl.finish([d / c for c in j["cuts"]], d / "ending.mp4",
                      audio_for(j, d), d / "final_raw.mp4")
            apply_subs(j, d)
        write_job(jid, j)

    return run_task(jid, "엔딩 만들기", work)


# ---- 자막 -------------------------------------------------------------------

@app.post("/api/{jid}/subtitles")
def gen_subs(jid: str):
    """음원에서 가사 초안을 뽑는다. 노래는 자주 틀리므로 사람이 고치는 것을 전제로 한다."""
    d = job_dir(jid)
    job = read_job(jid)
    src = audio_for(job, d)
    if not src:
        raise HTTPException(400, "먼저 춤 영상이나 음악 파일을 올려주세요.")

    def work():
        j = read_job(jid)
        task_set(jid, msg="가사를 받아쓰는 중")
        j["subs"] = pl.transcribe(src)
        j["subs_on"] = True
        j["spent"] = round(j.get("spent", 0) + pl.STT_PRICE, 3)
        write_job(jid, j)

    return run_task(jid, "가사 자막 만들기", work)


@app.post("/api/{jid}/subtitles/save")
def save_subs(jid: str, subs: str = Form("[]"), subs_on: str = Form("true")):
    """고친 자막을 저장한다. 이미 만든 영상이 있으면 자막만 다시 굽는다 — 무료."""
    d = job_dir(jid)
    job = read_job(jid)
    try:
        items = json.loads(subs)
    except Exception:
        raise HTTPException(400, "자막 형식이 잘못됐습니다.")
    job["subs"] = [{"start": float(x.get("start", 0)), "end": float(x.get("end", 0)),
                    "text": str(x.get("text", ""))} for x in items]
    job["subs_on"] = (subs_on == "true")
    write_job(jid, job)

    if job.get("result") and (d / "final_raw.mp4").exists():
        def work():
            j = read_job(jid)
            task_set(jid, msg="자막 다시 굽는 중")
            apply_subs(j, d)
            write_job(jid, j)
        return run_task(jid, "자막 반영", work)
    return job


def apply_subs(j, d):
    """원본(final_raw)에 카메라 무빙과 자막을 입혀 최종본을 만든다.

    생성이 아니라 후처리라서 몇 번이든 무료로 다시 할 수 있다.
    카메라를 먼저 걸고 자막을 나중에 얹어야 자막이 같이 흔들리지 않는다.
    """
    import shutil as _sh
    raw = d / "final_raw.mp4"
    cam = d / "final_cam.mp4"
    pl.camera_move(raw, cam, j.get("camera", "normal"))
    if j.get("subs_on") and j.get("subs"):
        pl.burn_subs(cam, j["subs"], d / "final.mp4",
                     style=j.get("subs_style", "soft"),
                     font=(j.get("subs_font") or None),
                     size=(j.get("subs_size") or None),
                     pos=j.get("subs_pos", "top"),
                     color=j.get("subs_color"),
                     outline_color=j.get("subs_outline_color"),
                     outline=j.get("subs_outline"),
                     bold=j.get("subs_bold"))
    else:
        _sh.copy(str(cam), str(d / "final.mp4"))
    j["result"] = "final.mp4"


@app.post("/api/{jid}/subtitles/restyle")
def restyle_subs(jid: str):
    """카메라 무빙과 자막 모양을 다시 입힌다. 생성이 아니라 무료다."""
    d = job_dir(jid)
    job = read_job(jid)
    if not (d / "final_raw.mp4").exists():
        raise HTTPException(400, "먼저 영상을 만들어주세요.")

    def work():
        j = read_job(jid)
        task_set(jid, msg="카메라와 자막 다시 입히는 중")
        apply_subs(j, d)
        write_job(jid, j)

    return run_task(jid, "모양 반영", work)


# ---- 5~7. 생성 ---------------------------------------------------------------

@app.get("/api/{jid}/estimate")
def estimate(jid: str):
    job = read_job(jid)
    n = job.get("segments") or 0
    d = RUNS / jid
    secs = pl._dur(d / job["plate"]) if job.get("plate") else n * pl.CUT_SEC
    if not job.get("one_shot", True):
        secs = n * pl.CUT_SEC
    dance = round(secs * vd.PRICE_PER_SEC, 2)
    ending = round(pl.NB_PRICE + pl.POSTER_VID_PRICE, 2)
    return {"segments": n, "seconds": round(secs), "one_shot": job.get("one_shot", True),
            "dance": dance,
            "ending": ending, "total": round(dance + ending, 2)}


@app.post("/api/{jid}/render")
def render(jid: str, with_ending: str = Form("true"), confirm_low: str = Form("false")):
    d = job_dir(jid)
    job = read_job(jid)
    if not job.get("keycut_approved"):
        raise HTTPException(400, "키컷을 먼저 확인하고 승인해주세요.")
    if not job.get("plate"):
        raise HTTPException(400, "레퍼런스 춤 영상을 먼저 올려주세요.")
    chk = score_check(job)
    if chk and chk["level"] == "block" and confirm_low != "true":
        raise HTTPException(409, {"low_score": chk})

    # 같은 영상을 몇 번이나 다시 만들었는지, 오늘 몇 편을 만들었는지 본다
    done_n = int(job.get("render_n", 0))
    if done_n > REDO_LIMIT:
        raise HTTPException(429, "이 영상은 다시 만들기를 %d번까지 할 수 있습니다. "
                                 "자막·글꼴·카메라는 다시 만들지 않아도 바꿀 수 있습니다."
                                 % REDO_LIMIT)
    me, ip = sid(), client_ip()
    if quota_left() <= 0:
        raise HTTPException(429, "오늘 만들 수 있는 %d편을 다 쓰셨습니다. "
                                 "내일 다시 이용해 주세요." % DAILY_VIDEOS)
    usage_bump(me, 1)
    if ip:
        usage_bump("ip:" + ip, 1)
    want_ending = (with_ending == "true")

    def work():
        try:
            _submit_cuts(jid, d, want_ending)
        except Exception:
            # 실패한 것은 쓴 것으로 치지 않는다. 하루 편수도, 이 영상의
            # 다시 만들기 횟수도 되돌린다 (안 그러면 실패만으로 막힌다).
            usage_bump(me, -1)
            if ip:
                usage_bump("ip:" + ip, -1)
            try:
                jj = read_job(jid)
                jj["render_n"] = max(0, int(jj.get("render_n", 1)) - 1)
                write_job(jid, jj)
            except Exception:
                pass
            raise

    return run_task(jid, "영상 만들기", work)


def _submit_cuts(jid, d, want_ending):
    """춤 영상 생성을 fal 에 맡기기만 한다. 결과는 나중에 받는다."""
    j = read_job(jid)
    j["render_n"] = int(j.get("render_n", 0)) + 1
    key = str(d / j["keycut"])
    plate = d / j["plate"]

    if j.get("one_shot", True):
        task_set(jid, msg="춤 영상 생성을 맡기는 중", done=0, total=1)
        (d / "cuts").mkdir(parents=True, exist_ok=True)
        rid = vd.submit(key, str(plate), orientation="video")
        reqs = [{"id": rid, "out": "cuts/full.mp4", "done": False}]
        secs = pl._dur(plate)
    else:
        task_set(jid, msg="레퍼런스를 %d초씩 나누는 중" % pl.CUT_SEC)
        segs = pl.split_plate(plate, d / "segs")
        (d / "cuts").mkdir(parents=True, exist_ok=True)
        task_set(jid, msg="춤 영상 생성을 맡기는 중", done=0, total=len(segs))
        reqs = []
        for n, seg in enumerate(segs, 1):
            reqs.append({"id": vd.submit(key, str(seg), orientation="video"),
                         "out": "cuts/cut%d.mp4" % n, "done": False})
        secs = len(segs) * pl.CUT_SEC

    j["pending"] = {"stage": "cuts", "reqs": reqs, "engine": vd.DEFAULT_ENGINE,
                    "want_ending": bool(want_ending), "secs": secs}
    j["spent"] = round(j.get("spent", 0) + secs * vd.PRICE_PER_SEC, 3)
    write_job(jid, j)
    task_set(jid, state="waiting", label="영상 만들기", done=0, total=len(reqs),
             msg="춤 영상 만드는 중 (8~12분) — 창을 닫아도 계속됩니다",
             started=time.time())


def _collect_cuts(jid, d):
    """맡긴 일이 다 됐는지 확인하고, 다 됐으면 받아온다.

    화면이 진행 상황을 물어볼 때 불린다. fal 에 너무 자주 묻지 않도록
    CHECK_SEC 초에 한 번만 확인한다.
    """
    j = read_job(jid)
    p = j.get("pending")
    if not p or p.get("stage") != "cuts":
        return
    eng = p.get("engine") or vd.DEFAULT_ENGINE
    plate = d / j["plate"] if j.get("plate") else None
    changed = False
    for r in p["reqs"]:
        if r.get("done"):
            continue
        st = vd.poll(r["id"], engine=eng)
        if st == "failed":
            _fail(jid, d, "생성 모델이 이 영상을 만들지 못했습니다. 다시 시도해 주세요.")
            return
        if st == "done":
            out = d / r["out"]
            out.parent.mkdir(parents=True, exist_ok=True)
            vd.fetch(r["id"], str(out), engine=eng, fallback=plate)
            r["done"] = True
            changed = True
    if changed:
        j["pending"] = p
        write_job(jid, j)
    done_n = sum(1 for r in p["reqs"] if r.get("done"))
    task_set(jid, done=done_n, total=len(p["reqs"]))
    if done_n < len(p["reqs"]):
        return

    # 다 받았다. 여기서부터는 짧은 일이라 스레드로 마무리한다.
    j["cuts"] = [r["out"] for r in p["reqs"]]
    j["pending"] = {"stage": "finishing", "want_ending": p.get("want_ending", True)}
    write_job(jid, j)
    threading.Thread(target=_finish_up, args=(jid, d, j.get("owner") or ""),
                     daemon=True).start()


def _finish_up(jid, d, owner):
    """엔딩을 붙이고 합치고 자막을 굽는다. 몇 십 초 걸린다.

    주인을 인자로 받는다 — 작업을 읽으려면 세션이 먼저 있어야 하는데,
    새 스레드에는 쿠키가 없어서 읽고 나서 세우면 이미 늦는다.
    """
    _SID.set(owner)
    try:
        j = read_job(jid)
        want_ending = (j.get("pending") or {}).get("want_ending", True)
        task_set(jid, state="running", msg="영상 합치는 중", done=0, total=0)

        ending = None
        if want_ending:
            if j.get("ending") and (d / j["ending"]).exists():
                ending = j["ending"]      # 이미 확인한 엔딩은 다시 뽑지 않는다
            else:
                task_set(jid, msg="엔딩 장면 그리는 중")
                ending = build_ending(j, d, jid)
            write_job(jid, j)

        pl.finish([d / c for c in j["cuts"]], (d / ending) if ending else None,
                  audio_for(j, d), d / "final_raw.mp4")
        if j.get("subs_on") and j.get("subs"):
            task_set(jid, msg="자막 굽는 중")
        apply_subs(j, d)
        j["pending"] = None
        write_job(jid, j)
        sync_up(jid, d)
        task_set(jid, state="done", msg=None, at=time.time())
    except Exception as e:
        traceback.print_exc()
        _fail(jid, d, friendly(e))


def _fail(jid, d, msg):
    """실패 처리. 쓴 것으로 치지 않고 되돌린다."""
    try:
        j = read_job(jid)
        j["pending"] = None
        j["render_n"] = max(0, int(j.get("render_n", 1)) - 1)
        write_job(jid, j)
        who = j.get("owner") or ""
        if who:
            usage_bump(who, -1)
    except Exception:
        pass
    task_set(jid, state="error", msg=msg, at=time.time())


class NoCacheStatic(StaticFiles):
    """브라우저가 옛 화면을 계속 쓰면 고친 코드가 안 돈다. 캐시를 끈다."""

    def file_response(self, *args, **kw):
        r = super().file_response(*args, **kw)
        r.headers["Cache-Control"] = "no-store, must-revalidate"
        r.headers["Pragma"] = "no-cache"
        return r


app.mount("/", NoCacheStatic(directory=str(APP / "static_studio"), html=True), name="static")


if __name__ == "__main__":
    import uvicorn
    # 서버에 올릴 때는 HOST=0.0.0.0 을 줘야 바깥에서 들어올 수 있다.
    # 내 컴퓨터에서 돌릴 때는 기본값(127.0.0.1)이라 바깥에 열리지 않는다.
    uvicorn.run(app,
                host=os.environ.get("HOST", "127.0.0.1"),
                port=int(os.environ.get("PORT", "8020")))
