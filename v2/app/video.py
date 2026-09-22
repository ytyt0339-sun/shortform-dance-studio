# -*- coding: utf-8 -*-
"""4단계 — 모션 전이

엔진이 둘이다.

Kling 모션컨트롤
  - 레퍼런스는 1인 단독이어야 한다. 다인 군무는 422로 거부된다.
  - 출력 구도는 캐릭터 이미지 구도를 따라간다 (입력 60% -> 출력 76%).
  - 동작이 클수록 캐릭터 비율이 변형된다. 이건 설정으로 못 막는 트레이드오프.

Seedance reference-to-video
  - 참조를 프롬프트 안에서 @Image1 / @Video1 로 가리킨다.
  - 이미지를 여러 장 넣을 수 있어 한 번에 여러 캐릭터를 만들 수 있다.
  - Kling 의 "1인 단독" 제약이 문서에 없다.
  - 대신 초당 단가가 Kling 의 2~4배다.
"""
import os
import time
import urllib.request

ENGINES = {
    "kling": dict(
        label="Kling 모션컨트롤",
        endpoint="fal-ai/kling-video/v2.6/standard/motion-control",
        min_sec=3, max_sec=30, per_sec=0.07,
        note="가장 싸다. 레퍼런스에 사람이 한 명만 나와야 한다."),
    "seedance-fast": dict(
        label="Seedance 2.0 fast",
        endpoint="bytedance/seedance-2.0/fast/reference-to-video",
        min_sec=4, max_sec=15, per_sec=0.1452,
        note="캐릭터를 여러 명 넣을 수 있다. Kling 보다 2배 비싸다."),
    "seedance": dict(
        label="Seedance 2.5",
        endpoint="bytedance/seedance-2.5/reference-to-video",
        min_sec=4, max_sec=30, per_sec=0.2838,
        note="가장 좋은 화질(1080p 가능). Kling 보다 4배 비싸다."),
}
DEFAULT_ENGINE = "kling"

# 예전 코드가 참조하던 이름들 — 그대로 둔다
ENDPOINT = ENGINES["kling"]["endpoint"]
PRICE_PER_SEC = ENGINES["kling"]["per_sec"]

# 480p 는 픽셀 수가 720p 의 약 45% 라 그만큼 싸다
_RES_MULT = {"480p": 0.45, "720p": 1.0, "1080p": 2.25}

# Seedance 기본 프롬프트. 캐릭터를 지키라고 반복해서 못박는다.
SEEDANCE_PROMPT = (
    "The character in @Image1 performs the exact dance movements from @Video1. "
    "Keep the character's appearance, proportions, head-to-body ratio, colors and "
    "outfit strictly identical to @Image1 in every frame. "
    "Full body visible from head to feet, single character, centered, "
    "plain flat background, fixed camera, no cuts.")


def cost(seconds, engine=DEFAULT_ENGINE, resolution="720p"):
    e = ENGINES.get(engine) or ENGINES[DEFAULT_ENGINE]
    m = 1.0 if engine == "kling" else _RES_MULT.get(resolution, 1.0)
    return round(seconds * e["per_sec"] * m, 3)


def _client():
    import fal_client
    if not os.environ.get("FAL_KEY"):
        raise RuntimeError("FAL_KEY가 설정되지 않았습니다.")
    return fal_client


def generate(image_path, video_path, out_path, orientation="video", prompt=None,
             engine=DEFAULT_ENGINE, resolution="720p", seconds=None, images=None):
    """캐릭터 이미지 + 레퍼런스 플레이트 -> 캐릭터가 같은 동작을 하는 영상.

    images 에 경로 목록을 주면 Seedance 에서 여러 캐릭터를 한 번에 넣는다
    (@Image1, @Image2 ...). Kling 은 한 장만 받으므로 첫 장만 쓴다.
    """
    fal_client = _client()
    spec = ENGINES.get(engine) or ENGINES[DEFAULT_ENGINE]
    paths = list(images) if images else [image_path]

    if engine == "kling":
        args = {
            "image_url": fal_client.upload_file(paths[0]),
            "video_url": fal_client.upload_file(video_path),
            "character_orientation": orientation,   # video=영상 방향 따라감
            "keep_original_sound": False,
        }
        if prompt and prompt.strip():
            args["prompt"] = prompt.strip()
    else:
        args = {
            "prompt": (prompt.strip() if prompt and prompt.strip() else SEEDANCE_PROMPT),
            "image_urls": [fal_client.upload_file(p) for p in paths],
            "video_urls": [fal_client.upload_file(video_path)],
            "resolution": resolution,
            "aspect_ratio": "9:16",
            "generate_audio": False,     # 음악은 편집 단계에서 붙인다
        }
        if seconds:
            n = max(spec["min_sec"], min(spec["max_sec"], int(round(seconds))))
            args["duration"] = str(n)

    t0 = time.time()
    res = fal_client.subscribe(spec["endpoint"], arguments=args, with_logs=False)
    v = res.get("video") or {}
    if not v.get("url"):
        raise RuntimeError(f"영상이 반환되지 않았습니다: {str(res)[:200]}")
    urllib.request.urlretrieve(v["url"], out_path)
    return round(time.time() - t0, 1), v["url"]


# ---- 대기열 방식 -------------------------------------------------------------
# generate() 는 결과가 나올 때까지 8~12분을 붙잡고 서 있는다. 사람이 몰리면
# 그 시간만큼 서버가 묶인다. 그래서 "맡기고(submit) -> 가끔 확인하고(poll) ->
# 다 되면 받아오는(fetch)" 세 조각으로 나눈다. 서버가 중간에 재시작돼도
# 맡긴 번호만 들고 있으면 이어서 받을 수 있다.
#
# DRY_RUN=1 이면 fal 을 부르지 않고 흉내만 낸다 (돈 안 쓰고 흐름을 확인할 때).

_DRY = {}          # 흉내 낼 때 쓰는 접수 시각
DRY_SEC = float(os.environ.get("DRY_SEC") or 6)


def _args_for(engine, image_path, video_path, orientation, prompt, resolution,
              seconds, images):
    fal_client = _client()
    spec = ENGINES.get(engine) or ENGINES[DEFAULT_ENGINE]
    paths = list(images) if images else [image_path]
    if engine == "kling":
        args = {
            "image_url": fal_client.upload_file(paths[0]),
            "video_url": fal_client.upload_file(video_path),
            "character_orientation": orientation,
            "keep_original_sound": False,
        }
        if prompt and prompt.strip():
            args["prompt"] = prompt.strip()
    else:
        args = {
            "prompt": (prompt.strip() if prompt and prompt.strip() else SEEDANCE_PROMPT),
            "image_urls": [fal_client.upload_file(p) for p in paths],
            "video_urls": [fal_client.upload_file(video_path)],
            "resolution": resolution,
            "aspect_ratio": "9:16",
            "generate_audio": False,
        }
        if seconds:
            n = max(spec["min_sec"], min(spec["max_sec"], int(round(seconds))))
            args["duration"] = str(n)
    return spec["endpoint"], args


def submit(image_path, video_path, orientation="video", prompt=None,
           engine=DEFAULT_ENGINE, resolution="720p", seconds=None, images=None):
    """생성을 맡기고 접수 번호만 받는다. 기다리지 않는다."""
    if os.environ.get("DRY_RUN") == "1":
        rid = "dry-%d" % (len(_DRY) + 1)
        _DRY[rid] = time.time()
        return rid
    fal_client = _client()
    endpoint, args = _args_for(engine, image_path, video_path, orientation,
                               prompt, resolution, seconds, images)
    return fal_client.submit(endpoint, arguments=args).request_id


def poll(request_id, engine=DEFAULT_ENGINE):
    """접수한 일이 어디까지 갔는지. 'queued' / 'running' / 'done' / 'failed'."""
    if str(request_id).startswith("dry-"):
        t0 = _DRY.get(request_id, 0)
        return "done" if time.time() - t0 > DRY_SEC else "running"
    fal_client = _client()
    spec = ENGINES.get(engine) or ENGINES[DEFAULT_ENGINE]
    try:
        st = fal_client.status(spec["endpoint"], request_id, with_logs=False)
    except Exception as e:
        # 잠깐 끊긴 것과 진짜 실패를 구분한다. 확실하지 않으면 계속 기다린다.
        if "404" in str(e):
            return "failed"
        return "running"
    name = type(st).__name__.lower()
    if "completed" in name:
        return "done"
    if "queued" in name:
        return "queued"
    return "running"


def fetch(request_id, out_path, engine=DEFAULT_ENGINE, fallback=None):
    """다 된 결과를 내려받는다."""
    if str(request_id).startswith("dry-"):
        import shutil as _sh
        if not fallback:
            raise RuntimeError("흉내 실행에는 대신 쓸 영상(fallback)이 필요합니다.")
        _sh.copy(str(fallback), str(out_path))
        return "dry://" + str(request_id)
    fal_client = _client()
    spec = ENGINES.get(engine) or ENGINES[DEFAULT_ENGINE]
    res = fal_client.result(spec["endpoint"], request_id)
    v = res.get("video") or {}
    if not v.get("url"):
        raise RuntimeError("영상이 반환되지 않았습니다: %s" % str(res)[:200])
    urllib.request.urlretrieve(v["url"], out_path)
    return v["url"]


def motion_amount(path, fps=5, limit=40):
    """프레임간 평균 밝기 변화. 캐릭터가 실제로 움직였는지 확인용 지표."""
    import subprocess, tempfile, shutil
    import numpy as np
    from PIL import Image
    d = tempfile.mkdtemp()
    try:
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", path,
                        "-vf", f"fps={fps},scale=160:-1", os.path.join(d, "f%03d.png")],
                       check=True)
        fs = sorted(os.listdir(d))[:limit]
        if len(fs) < 2:
            return None
        a = [np.asarray(Image.open(os.path.join(d, f)).convert("L")).astype(float) for f in fs]
        return round(float(np.mean([np.abs(a[i + 1] - a[i]).mean()
                                    for i in range(len(a) - 1)])), 2)
    finally:
        shutil.rmtree(d, ignore_errors=True)


def thumb_sheet(path, out, n=5, h=240):
    """결과 확인용 가로 스트립."""
    import subprocess, tempfile, shutil
    from PIL import Image
    import json
    d = tempfile.mkdtemp()
    try:
        dur = float(subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "csv=p=0", path], capture_output=True, text=True).stdout.strip() or 6)
        ims = []
        for i in range(n):
            t = dur * (i + .5) / n
            p = os.path.join(d, f"{i}.png")
            subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", str(t), "-i", path,
                            "-vframes", "1", p], check=True)
            im = Image.open(p).convert("RGB")
            ims.append(im.resize((int(im.width * h / im.height), h), Image.LANCZOS))
        sh = Image.new("RGB", (sum(i.width for i in ims) + 4 * (len(ims) + 1), h + 8),
                       (255, 255, 255))
        x = 4
        for im in ims:
            sh.paste(im, (x, 4))
            x += im.width + 4
        sh.save(out, "JPEG", quality=85)
        return out
    finally:
        shutil.rmtree(d, ignore_errors=True)
