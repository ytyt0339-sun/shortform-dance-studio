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
from fastapi.responses import FileResponse, JSONResponse
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
# 이 시간이 지난 작업은 자동으로 지운다. 배포한 곳에서 KEEP_HOURS 로 바꿀 수 있다.
KEEP_HOURS = int(os.environ.get("KEEP_HOURS") or 24 * 14)
SWEEP_MIN = 30           # 청소 주기


@app.middleware("http")
async def session_mw(request, call_next):
    sid = request.cookies.get(SID_COOKIE) or ""
    fresh = not sid
    if fresh:
        sid = uuid.uuid4().hex
    token = _SID.set(sid)
    try:
        resp = await call_next(request)
    finally:
        _SID.reset(token)
    if fresh:
        resp.set_cookie(SID_COOKIE, sid, max_age=60 * 60 * 24 * 30,
                        httponly=True, samesite="lax")
    return resp


def sid():
    return _SID.get()


def sweep():
    """오래된 작업을 지운다. 결과물은 받아가는 것으로 끝이라 쌓아둘 이유가 없다."""
    cut = time.time() - KEEP_HOURS * 3600
    for d in list(RUNS.iterdir()):
        try:
            f = d / "job.json"
            if not f.exists() or f.stat().st_mtime < cut:
                shutil.rmtree(d, ignore_errors=True)
        except Exception:
            pass


def _sweeper():
    while True:
        time.sleep(SWEEP_MIN * 60)
        try:
            sweep()
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


def job_dir(jid):
    """작업 폴더. 남의 작업은 없는 것처럼 취급한다."""
    d = RUNS / jid
    f = d / "job.json"
    if not d.exists() or not f.exists():
        raise HTTPException(404, "작업을 찾을 수 없습니다.")
    try:
        owner = json.loads(f.read_text(encoding="utf-8")).get("owner")
    except Exception:
        owner = None
    if owner and owner != sid():
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


# ---- 긴 작업 -----------------------------------------------------------------
# 키컷 생성은 20초, 영상 생성은 6분씩 걸린다. HTTP 요청 하나로 붙잡고 있으면
# 연결이 끊기는 순간 실패하므로 스레드로 돌리고 진행률만 폴링한다.
TASKS = {}
LOCK = threading.Lock()


def task_set(jid, **kw):
    with LOCK:
        t = TASKS.setdefault(jid, {})
        t.update(kw)
        return dict(t)


def task_get(jid):
    with LOCK:
        return dict(TASKS.get(jid) or {"state": "idle"})


def run_task(jid, label, fn):
    if task_get(jid).get("state") == "running":
        raise HTTPException(409, "이미 다른 작업이 진행 중입니다.")

    owner = sid()      # 요청을 보낸 사람

    def worker():
        # 작업 스레드에는 쿠키가 없어 세션이 비어 있다. 그대로 두면 job_dir 의
        # 주인 확인에 걸려 자기 작업인데도 404 가 난다.
        _SID.set(owner)
        try:
            fn()
            task_set(jid, state="done", msg=None, at=time.time())
        except Exception as e:
            traceback.print_exc()
            task_set(jid, state="error", msg=friendly(e), at=time.time())

    task_set(jid, state="running", label=label, msg=None, done=0, total=0, started=time.time())
    threading.Thread(target=worker, daemon=True).start()
    return task_get(jid)


def friendly(e):
    s = str(e)
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
        "spent": 0.0,
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
        if j.get("owner") and j.get("owner") != me:
            continue
        out.append({"id": j["id"], "title": j.get("title"), "created": j["created"],
                    "result": bool(j.get("result")), "spent": j.get("spent", 0)})
    return out


@app.get("/api/{jid}")
def get_job(jid: str):
    job = read_job(jid)
    job["score_check"] = score_check(job)
    return job


@app.get("/api/{jid}/task")
def get_task(jid: str):
    return task_get(jid)


@app.delete("/api/{jid}")
def del_job(jid: str):
    shutil.rmtree(job_dir(jid), ignore_errors=True)
    return {"ok": True}


@app.get("/api/{jid}/file/{name}")
def get_file(jid: str, name: str):
    p = job_dir(jid) / name
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
    return FileResponse(job_dir(jid) / job["result"], media_type="video/mp4",
                        filename="%s.mp4" % (job.get("title") or "video"))


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
    want_ending = (with_ending == "true")

    def work():
        j = read_job(jid)
        key = d / j["keycut"]

        secs = pl._dur(d / j["plate"])
        # 카메라 무빙은 생성 후에 입힌다. Kling 에 직접 카메라를 지시하는 길도 있지만
        # 얼마나 따르는지 확인된 적이 없고, 마음에 안 들면 다시 뽑아야 해서 뺐다.
        cam_prompt = None
        if j.get("one_shot", True):
            task_set(jid, msg="춤 영상 생성 중 — 끊김 없이 한 번에 (8~12분)", done=0, total=1)
            out = d / "cuts" / "full.mp4"
            out.parent.mkdir(parents=True, exist_ok=True)
            pl.kling_single(str(key), d / j["plate"], out, prompt=cam_prompt,
                            progress=lambda i, n: task_set(jid, done=i, total=n))
            j["cuts"] = ["cuts/full.mp4"]
        else:
            task_set(jid, msg="레퍼런스를 %d초씩 나누는 중" % pl.CUT_SEC)
            segs = pl.split_plate(d / j["plate"], d / "segs")
            task_set(jid, msg="춤 영상 생성 중 (한 컷에 5~7분, 동시 진행)", done=0, total=len(segs))
            cuts = pl.kling_cuts(str(key), segs, d / "cuts", prompt=cam_prompt,
                                 progress=lambda i, n: task_set(jid, done=i, total=n))
            j["cuts"] = [str(Path(c).relative_to(d)).replace("\\", "/") for c in cuts]
            secs = len(segs) * pl.CUT_SEC
        j["spent"] = round(j.get("spent", 0) + secs * vd.PRICE_PER_SEC, 3)
        write_job(jid, j)

        ending = None
        if want_ending:
            if j.get("ending") and (d / j["ending"]).exists():
                ending = j["ending"]          # 이미 만들어 확인한 엔딩은 다시 뽑지 않는다
            else:
                task_set(jid, msg="엔딩 장면 그리는 중", done=0, total=0)
                ending = build_ending(j, d, jid)
            write_job(jid, j)

        task_set(jid, msg="영상 합치는 중", done=0, total=0)
        pl.finish([d / c for c in j["cuts"]], (d / ending) if ending else None,
                  audio_for(j, d), d / "final_raw.mp4")
        if j.get("subs_on") and j.get("subs"):
            task_set(jid, msg="자막 굽는 중")
        apply_subs(j, d)
        write_job(jid, j)

    return run_task(jid, "영상 만들기", work)


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
