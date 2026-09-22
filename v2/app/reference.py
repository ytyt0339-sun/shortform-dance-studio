# -*- coding: utf-8 -*-
"""3단계 — 레퍼런스 영상 적합도 판정 + 플레이트 생성

실측으로 확인된 것들을 그대로 반영한다.
  - Kling은 1인 단독이 아니면 거부한다 ("연속 유효 동작 3초 미만").
  - 동작이 작으면 캐릭터가 거의 움직이지 않는다 (루터스 손허리 구간 = 0.90).
  - 인스타 UI·검은 여백은 잘라내야 인물이 커진다.
"""
import os
import re
import subprocess
import numpy as np

MODEL = "pose_landmarker_full.task"
SAMPLE_FPS = 15
PLATE_FPS = 16          # Kling 과금이 16프레임=1초 기준이라 여기 맞춘다
PLATE_W, PLATE_H = 612, 1088
MAX_DUR = 30.0      # Kling: character_orientation="video" 기준 최대 30초
MIN_DUR = 3.0       # 3초 미만이면 "연속 유효 동작 부족"으로 거부된다

J = dict(nose=0, l_sh=11, r_sh=12, l_wr=15, r_wr=16,
         l_hip=23, r_hip=24, l_an=27, r_an=28)
KEY = [J['nose'], J['l_sh'], J['r_sh'], J['l_wr'], J['r_wr'],
       J['l_hip'], J['r_hip'], J['l_an'], J['r_an']]

# 하드 컷오프 — 이 아래면 다른 항목이 아무리 좋아도 탈락시킨다.
# 가중합만 쓰면 '전신노출 2%인데 72.9점' 같은 결과가 나온다.
CUTOFF = dict(전신노출=0.55, 단독인물=0.80, 검출안정성=0.80)
WEIGHT = dict(전신노출=.24, 화면내유지=.14, 검출안정성=.10, 검출신뢰도=.06,
              단독인물=.18, 동작연속성=.10, 컷전환없음=.06, 동작크기=.12)


def model_path(app_dir):
    return os.path.join(app_dir, MODEL)


def ensure_model(app_dir):
    """포즈 모델이 없으면 내려받는다 (9MB, 최초 1회)."""
    p = model_path(app_dir)
    if not os.path.exists(p):
        import urllib.request
        url = ("https://storage.googleapis.com/mediapipe-models/pose_landmarker/"
               "pose_landmarker_full/float16/latest/pose_landmarker_full.task")
        urllib.request.urlretrieve(url, p)
    return p


def probe(path):
    """영상 정보. width/height 는 ffmpeg 이 실제로 뱉는 '표시 크기' 기준이다.

    휴대폰 영상은 rotation=90 메타데이터를 갖는 경우가 많은데, ffprobe 의
    stream width/height 는 회전 전 값이라 ffmpeg 출력과 어긋난다. 크롭 좌표를
    ffmpeg 에 넘기므로 반드시 회전을 반영해야 한다.
    """
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v",
         "-show_entries", "stream=width,height,duration,r_frame_rate",
         "-of", "default=nw=1:nk=1", path],
        capture_output=True, text=True).stdout.split()
    w, h = int(out[0]), int(out[1])
    fr = out[2] if "/" in out[2] else out[3]
    num, den = (fr.split("/") + ["1"])[:2]
    fps = float(num) / float(den or 1)
    dur = float(out[3] if "/" not in out[3] else out[2])

    rot = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v",
         "-show_entries", "stream_side_data=rotation",
         "-of", "default=nw=1:nk=1", path],
        capture_output=True, text=True).stdout.strip()
    try:
        if abs(int(float(rot.splitlines()[0]))) % 180 == 90:
            w, h = h, w
    except (ValueError, IndexError):
        pass
    return dict(width=w, height=h, fps=round(fps, 2), duration=round(dur, 2))


def detect_content_box(path, t=None, dark=28, pad=2):
    """검은 여백(레터박스)을 빼고 실제 영상 내용이 있는 영역을 찾는다.

    프레임은 반드시 ffmpeg 으로 뽑는다. 휴대폰 영상은 rotation=90 같은 메타데이터를
    갖는데, ffmpeg 은 회전을 적용하고 OpenCV 는 적용하지 않아 가로/세로가 뒤바뀐다.
    크롭 좌표는 나중에 ffmpeg 이 쓰므로 ffmpeg 기준으로 재야 한다.
    """
    import tempfile, os as _os
    import numpy as _np
    from PIL import Image as _I
    info = probe(path)
    t = info["duration"] * 0.4 if t is None else t
    fd, tmp = tempfile.mkstemp(suffix=".png")
    _os.close(fd)
    try:
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", str(t), "-i", path,
                        "-vframes", "1", tmp], check=True)
        g = _np.asarray(_I.open(tmp).convert("RGB")).astype(float).mean(2)
    except Exception:
        return None
    finally:
        try:
            _os.unlink(tmp)
        except OSError:
            pass
    rows = np.where(g.mean(1) > dark)[0]
    cols = np.where(g.mean(0) > dark)[0]
    if len(rows) == 0 or len(cols) == 0:
        return None
    y0, y1 = max(0, rows[0] - pad), min(g.shape[0], rows[-1] + pad)
    x0, x1 = max(0, cols[0] - pad), min(g.shape[1], cols[-1] + pad)
    return dict(x=int(x0), y=int(y0), w=int(x1 - x0), h=int(y1 - y0))


def make_plate(src, out, start, dur, crop=None):
    """Kling 입력용 플레이트. 크롭 -> 9:16 레터박스(흰색) -> 16fps."""
    vf = []
    if crop:
        vf.append(f"crop={crop['w']}:{crop['h']}:{crop['x']}:{crop['y']}")
    vf += [
        f"scale={PLATE_W}:{PLATE_H}:force_original_aspect_ratio=decrease",
        f"pad={PLATE_W}:{PLATE_H}:(ow-iw)/2:(oh-ih)/2:color=white",
        f"fps={PLATE_FPS}", "setsar=1",
    ]
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-ss", str(start), "-t", str(dur), "-i", src,
         "-vf", ",".join(vf), "-an", "-c:v", "libx264", "-crf", "18",
         "-pix_fmt", "yuv420p", out], check=True)
    return out


def frame_metrics(path, app_dir, max_people=2, progress=None):
    """프레임별 원자료를 뽑는다. 한 번만 훑으면 어떤 구간이든 점수를 낼 수 있다."""
    import cv2
    import mediapipe as mp
    from mediapipe.tasks import python as mpp
    from mediapipe.tasks.python import vision

    # 경로에 한글이 있으면 MediaPipe(C++)가 파일을 열지 못한다.
    # 바이트로 읽어 buffer 로 넘기면 경로 문제가 사라진다.
    with open(ensure_model(app_dir), "rb") as f:
        blob = f.read()
    opts = vision.PoseLandmarkerOptions(
        base_options=mpp.BaseOptions(model_asset_buffer=blob),
        running_mode=vision.RunningMode.VIDEO, num_poses=max_people,
        min_pose_detection_confidence=0.5, min_tracking_confidence=0.5)

    cap = cv2.VideoCapture(path)
    fps = cap.get(cv2.CAP_PROP_FPS) or 30
    total = cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0
    step = max(1, round(fps / SAMPLE_FPS))
    seq, npl, idx = [], [], 0
    with vision.PoseLandmarker.create_from_options(opts) as lm:
        while True:
            ok, bgr = cap.read()
            if not ok:
                break
            if idx % step:
                idx += 1
                continue
            img = mp.Image(image_format=mp.ImageFormat.SRGB,
                           data=cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB))
            res = lm.detect_for_video(img, int(idx / fps * 1000))
            npl.append(len(res.pose_landmarks))
            if res.pose_landmarks:
                p = res.pose_landmarks[0]
                seq.append(np.array([[k.x, k.y, k.visibility] for k in p], np.float32))
            else:
                seq.append(np.full((33, 3), np.nan, np.float32))
            idx += 1
            if progress and total and idx % (step * 20) == 0:
                progress(idx, int(total))
    cap.release()
    if not seq:
        return None

    seq = np.stack(seq)
    npl = np.array(npl)
    det = ~np.isnan(seq[:, 0, 0])

    xy = seq[:, :, :2]
    inb = ((xy[:, KEY, 0] > .02) & (xy[:, KEY, 0] < .98) &
           (xy[:, KEY, 1] > .02) & (xy[:, KEY, 1] < .98))
    # 전신 노출은 '위치'로 판정한다.
    # MediaPipe 의 visibility 는 모션블러만 있어도 뚝 떨어져서, 춤이 격할수록
    # 점수가 낮아지는 거꾸로 된 지표가 된다. 손목 74% / 발목 88% 로 실측됨.
    with np.errstate(invalid="ignore"):
        span = (np.nanmean(xy[:, [J['l_an'], J['r_an']], 1], axis=1)
                - xy[:, J['nose'], 1])          # 코~발목 세로 길이
    full = inb.all(1) & (np.nan_to_num(span) > .35)
    # 검출 신뢰도는 별도 지표로 분리 (가중치를 낮게 준다)
    with np.errstate(invalid="ignore"):
        conf = np.nanmean(seq[:, KEY, 2], axis=1)
    d = np.linalg.norm(np.diff(xy, axis=0), axis=2)
    with np.errstate(invalid="ignore"):
        per = np.nanmean(d, axis=1)             # 프레임 간 이동량 (길이 n-1)

    # 컷 전환 시각. 구간마다 몇 번 있었는지 세려면 시각이 필요하다.
    out = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", path, "-vf",
         "select='gt(scene,0.35)',showinfo", "-f", "null", "-"],
        capture_output=True, text=True, errors="replace").stderr
    cut_times = [float(x) for x in re.findall(r"pts_time:([0-9.]+)", out)]

    return dict(full=full, inb=inb.all(1), det=det, conf=conf, npl=npl,
                per=per, cuts=cut_times, n=len(seq))


def score_window(m, i0=None, i1=None):
    """프레임별 지표에서 한 구간의 점수를 낸다. analyze 와 같은 식을 쓴다."""
    i0 = 0 if i0 is None else max(0, i0)
    i1 = m["n"] if i1 is None else min(m["n"], i1)
    if i1 - i0 < 2:
        return dict(ok=False, score=0, sub={}, fail=["구간이 너무 짧습니다."])
    per = m["per"][i0:max(i0, i1 - 1)]
    per = per[~np.isnan(per)]
    spike = (per > .06).mean() if len(per) else 1.
    move = float(np.median(per)) if len(per) else 0.
    dur = (i1 - i0) / SAMPLE_FPS
    t0, t1 = i0 / SAMPLE_FPS, i1 / SAMPLE_FPS
    cuts = sum(1 for t in m["cuts"] if t0 < t < t1)

    sub = {
        "전신노출": round(float(np.nan_to_num(m["full"][i0:i1]).mean()), 3),
        "화면내유지": round(float(np.nan_to_num(m["inb"][i0:i1]).mean()), 3),
        "검출안정성": round(float(m["det"][i0:i1].mean()), 3),
        "검출신뢰도": round(float(np.nan_to_num(m["conf"][i0:i1]).mean()), 3),
        "단독인물": round(float((m["npl"][i0:i1] <= 1).mean()), 3),
        "동작연속성": round(float(1 - spike), 3),
        "컷전환없음": round(float(max(0., 1 - (cuts / max(dur, 1e-6)) / .5)), 3),
        # 동작크기: 0.010 이상이면 만점. 손만 움직이는 구간은 여기서 걸린다.
        "동작크기": round(float(min(1., move / .010)), 3),
    }
    score = round(sum(sub[k] * WEIGHT[k] for k in WEIGHT) * 100, 1)

    fail = []
    for k, thr in CUTOFF.items():
        if sub[k] < thr:
            fail.append(f"{k} {sub[k]:.2f} (최소 {thr})")
    if sub["동작크기"] < 0.35:
        fail.append(f"동작이 너무 작습니다 ({move:.4f}) — 캐릭터가 거의 안 움직입니다")

    return dict(ok=not fail, score=score, sub=sub, fail=fail,
                motion=round(move, 4), cuts=cuts, frames=i1 - i0)


def analyze(path, app_dir, max_people=2):
    """플레이트를 채점한다. 반환: 점수 / 항목별 / 탈락사유."""
    m = frame_metrics(path, app_dir, max_people)
    if not m:
        return dict(ok=False, score=0, sub={}, fail=["영상을 읽지 못했습니다."])
    return score_window(m)


def recommend(path, app_dir, want=30.0, step=3.0, top=6, progress=None):
    """구간을 옮겨가며 점수를 내고 좋은 순서로 돌려준다.

    자동으로 고르지 않는다. 사용자가 보고 고르게 목록만 만든다.
    """
    m = frame_metrics(path, app_dir, progress=progress)
    if not m:
        return []
    total = m["n"] / SAMPLE_FPS
    want = min(want, total)
    w = max(2, int(round(want * SAMPLE_FPS)))
    hop = max(1, int(round(step * SAMPLE_FPS)))
    out = []
    for i0 in range(0, max(1, m["n"] - w + 1), hop):
        r = score_window(m, i0, i0 + w)
        out.append({"start": round(i0 / SAMPLE_FPS, 1),
                    "dur": round(w / SAMPLE_FPS, 1),
                    "score": r["score"], "ok": r["ok"],
                    "fail": r["fail"], "sub": r["sub"]})
    out.sort(key=lambda x: -x["score"])
    # 시작점이 너무 붙어 있는 것은 걸러 서로 다른 구간을 보여준다.
    # 짧은 영상에서는 고를 수 있는 폭 자체가 좁으니 간격도 좁힌다.
    span = max(0.0, total - want)
    gap = min(want * 0.4, span / max(1, top - 1)) if span > 0 else 0.0
    picked = []
    for c in out:
        if all(abs(c["start"] - q["start"]) >= gap for q in picked):
            picked.append(c)
        if len(picked) >= top:
            break
    picked.sort(key=lambda x: x["start"])
    return picked


def detect_subject_box(path, app_dir, samples=7, margin=0.14):
    """인물을 찾아 그 주변을 9:16 으로 잘라낼 영역을 계산한다.

    가로 영상(유튜브 튜토리얼 등)을 그대로 9:16 에 넣으면 위아래 여백이 커져
    인물이 화면의 25% 수준으로 작아진다. Kling 은 인물이 클수록 결과가 좋으므로
    먼저 인물 주변을 잘라낸다.
    """
    import tempfile, os as _os
    import mediapipe as mp
    from mediapipe.tasks import python as mpp
    from mediapipe.tasks.python import vision
    from PIL import Image as _I

    info = probe(path)
    W, H = info["width"], info["height"]
    with open(ensure_model(app_dir), "rb") as f:
        blob = f.read()
    opts = vision.PoseLandmarkerOptions(
        base_options=mpp.BaseOptions(model_asset_buffer=blob),
        running_mode=vision.RunningMode.IMAGE, num_poses=1,
        min_pose_detection_confidence=0.4)

    xs, ys = [], []
    fd, tmp = tempfile.mkstemp(suffix=".png")
    _os.close(fd)
    try:
        with vision.PoseLandmarker.create_from_options(opts) as lm:
            for k in range(samples):
                t = info["duration"] * (k + .5) / samples
                subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", str(t), "-i", path,
                                "-vframes", "1", tmp], check=True)
                arr = np.asarray(_I.open(tmp).convert("RGB"))
                res = lm.detect(mp.Image(image_format=mp.ImageFormat.SRGB, data=arr))
                if not res.pose_landmarks:
                    continue
                p = res.pose_landmarks[0]
                xs += [q.x for q in p]
                ys += [q.y for q in p]
    finally:
        try:
            _os.unlink(tmp)
        except OSError:
            pass
    if len(xs) < 20:
        return None

    x0, x1 = np.percentile(xs, 2), np.percentile(xs, 98)
    y0, y1 = np.percentile(ys, 2), np.percentile(ys, 98)
    cx, cy = (x0 + x1) / 2 * W, (y0 + y1) / 2 * H
    bh = (y1 - y0) * H * (1 + margin * 2)
    bw = bh * PLATE_W / PLATE_H                      # 9:16 로 맞춤
    need = (x1 - x0) * W * (1 + margin * 2)
    if bw < need:                                    # 팔을 벌리면 가로가 모자랄 수 있다
        bw = need
        bh = bw * PLATE_H / PLATE_W
    bw, bh = min(bw, W), min(bh, H)
    x = int(max(0, min(W - bw, cx - bw / 2)))
    y = int(max(0, min(H - bh, cy - bh / 2)))
    return dict(x=x, y=y, w=int(bw), h=int(bh))


def auto_crop(path, app_dir):
    """인물 기준 크롭을 우선하고, 실패하면 검은 여백 제거로 넘어간다."""
    try:
        box = detect_subject_box(path, app_dir)
        if box and box["w"] > 80 and box["h"] > 80:
            return box
    except Exception:
        pass
    return detect_content_box(path)

def count_people(path, app_dir, samples=9, max_people=5):
    """영상에 몇 명이 나오는지 센다. 업로드 시점에 여러 명을 걸러내기 위한 것.

    분석 단계의 '단독인물' 지표는 num_poses=2 라 옆 사람이 잘려 보이면 놓친다.
    여기서는 5명까지 감지해 확실히 센다.
    """
    import tempfile, os as _os
    import mediapipe as mp
    from mediapipe.tasks import python as mpp
    from mediapipe.tasks.python import vision
    from PIL import Image as _I

    info = probe(path)
    with open(ensure_model(app_dir), "rb") as f:
        blob = f.read()
    opts = vision.PoseLandmarkerOptions(
        base_options=mpp.BaseOptions(model_asset_buffer=blob),
        running_mode=vision.RunningMode.IMAGE, num_poses=max_people,
        min_pose_detection_confidence=0.45)

    counts, boxes = [], []
    fd, tmp = tempfile.mkstemp(suffix=".png")
    _os.close(fd)
    try:
        with vision.PoseLandmarker.create_from_options(opts) as lm:
            for k in range(samples):
                t = info["duration"] * (k + .5) / samples
                subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", str(t), "-i", path,
                                "-vframes", "1", tmp], check=True)
                res = lm.detect(mp.Image(image_format=mp.ImageFormat.SRGB,
                                         data=np.asarray(_I.open(tmp).convert("RGB"))))
                ps = res.pose_landmarks or []
                # 화면의 8% 이상 되는 인물만 센다 (배경의 아주 작은 사람은 무시)
                big = []
                for p in ps:
                    ys = [q.y for q in p]
                    if max(ys) - min(ys) > .08:
                        big.append(p)
                counts.append(len(big))
                if len(big) == 1:
                    xs = [q.x for q in big[0]]
                    ys = [q.y for q in big[0]]
                    boxes.append((min(xs), min(ys), max(xs), max(ys)))
    finally:
        try:
            _os.unlink(tmp)
        except OSError:
            pass
    if not counts:
        return dict(ok=False, people=0, solo_ratio=0.0,
                    reason="영상에서 사람을 찾지 못했습니다.")
    counts = np.array(counts)
    solo = float((counts == 1).mean())
    return dict(ok=solo >= .8, people=int(np.median(counts)),
                max_people=int(counts.max()), solo_ratio=round(solo, 2),
                reason=None if solo >= .8 else
                f"여러 명이 함께 나옵니다 (최대 {int(counts.max())}명 감지, "
                f"1명만 나오는 구간은 {solo*100:.0f}%).")
