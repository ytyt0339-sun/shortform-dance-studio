# -*- coding: utf-8 -*-
"""무거운 일들을 모아 둔 곳.

지금은 웹 서버가 이 함수들을 직접 부른다. 앞으로 영상 처리를 AWS Lambda 로
옮기면, 람다도 **같은 함수**를 부른다. 그래서 로직이 두 벌로 갈라지지 않는다.

약속
  d    작업 폴더 (Path). 필요한 파일이 이미 들어 있다고 본다.
  j    작업 기록 (dict). 고쳐서 돌려준다. 저장은 부르는 쪽이 한다.
  say  진행 상황 알림. 서버에서는 화면에 찍히고, 람다에서는 기록에 남는다.

이 파일은 웹이나 세션을 모르고, 파일과 기록만 다룬다. 그래야 어디서든 돈다.
"""
from pathlib import Path

import pipeline as pl
import reference as rf
import video as vd

APP = Path(__file__).resolve().parent


def _quiet(msg=None, done=None, total=None):
    """아무 데도 안 알리는 기본값."""
    return None


def analyze_reference(d, j, name, say=_quiet, app=None):
    """올라온 춤 영상을 세로로 자르고 점수를 낸다.

    만드는 것: plate.mp4 (세로로 자른 영상), plate_sheet.jpg (미리보기),
    소리가 있으면 ref_audio.mp3.
    """
    app = str(app or APP)
    d = Path(d)

    say(msg="인물을 찾아 세로로 자르는 중")
    box = rf.auto_crop(str(d / name), app)
    dur = min(rf.MAX_DUR, rf.probe(d / name)["duration"])
    rf.make_plate(str(d / name), str(d / "plate.mp4"), 0.0, dur, box)
    j["crop"] = box
    j["clip_start"] = 0.0
    j["clip_dur"] = round(dur, 1)
    j["seg_recs"] = None

    say(msg="적합도 채점 중")
    res = rf.analyze(str(d / "plate.mp4"), app)
    vd.thumb_sheet(str(d / "plate.mp4"), str(d / "plate_sheet.jpg"))
    j["plate"] = "plate.mp4"
    j["clip_score"] = res
    j["segments"] = max(1, int(dur // pl.CUT_SEC))

    # 레퍼런스에 소리가 있으면 뽑아둔다. 그 영상에서 동작을 따왔으므로 박자가 맞는다.
    if pl.has_audio(d / name):
        say(msg="레퍼런스 소리 추출 중")
        if pl.extract_audio(d / name, d / "ref_audio.mp3"):
            j["ref_audio"] = "ref_audio.mp3"
    return j


def scan_segments(d, j, say=_quiet, app=None):
    """구간마다 점수를 매겨 추천 목록을 만든다. 고르는 건 사용자가 한다."""
    app = str(app or APP)
    d = Path(d)
    src = d / j["reference"]
    want = min(rf.MAX_DUR, rf.probe(src)["duration"])

    # 실제로 만들어질 화면(세로 크롭)에서 재야 점수가 맞는다
    say(msg="세로로 잘라 훑는 중")
    box = j.get("crop") or rf.auto_crop(str(src), app)
    j["crop"] = box
    full = min(180.0, rf.probe(src)["duration"])
    rf.make_plate(str(src), str(d / "scan.mp4"), 0.0, full, box)

    say(msg="구간별로 점수 내는 중", done=0, total=0)
    recs = rf.recommend(str(d / "scan.mp4"), app, want=want, step=3.0, top=6,
                        progress=lambda i, n: say(done=i, total=n))
    (d / "scan.mp4").unlink(missing_ok=True)
    j["seg_recs"] = recs
    return j


def pick_segment(d, j, t0, say=_quiet, app=None):
    """사용자가 고른 구간으로 세로 영상을 다시 만든다."""
    app = str(app or APP)
    d = Path(d)
    src = d / j["reference"]
    info = rf.probe(src)
    dur = min(rf.MAX_DUR, max(rf.MIN_DUR, info["duration"] - t0))
    box = j.get("crop") or rf.auto_crop(str(src), app)

    say(msg="고른 구간으로 다시 자르는 중")
    rf.make_plate(str(src), str(d / "plate.mp4"), t0, dur, box)

    say(msg="적합도 채점 중")
    j["clip_score"] = rf.analyze(str(d / "plate.mp4"), app)
    vd.thumb_sheet(str(d / "plate.mp4"), str(d / "plate_sheet.jpg"))
    j["crop"] = box
    j["clip_start"] = round(t0, 1)
    j["clip_dur"] = round(dur, 1)
    j["segments"] = max(1, int(dur // pl.CUT_SEC))
    return j


def clean_character(d, j, feedback="", say=_quiet):
    """손그림 낙서를 캐릭터로 다듬는다. 원본은 따로 남긴다.

    원본에서 다시 다듬으므로, 여러 번 눌러도 고친 내용이 쌓여 뭉개지지 않는다.
    """
    d = Path(d)
    src = d / (j.get("character_raw") or j["character"])
    if not j.get("character_raw"):
        raw = d / ("raw" + src.suffix)
        raw.write_bytes(src.read_bytes())
        j["character_raw"] = raw.name
        src = raw

    say(msg="그림을 캐릭터로 다듬는 중")
    out = d / "character_clean.png"
    pl.clean_sketch(str(src), str(out), feedback=feedback)
    j["char_feedback"] = (feedback or "").strip()
    j["character"] = out.name
    j["character_n"] = j.get("character_n", 0) + 1
    j["keycut"] = None
    j["keycut_approved"] = False
    j["spent"] = round(j.get("spent", 0) + pl.NB_PRICE, 3)
    return j


def make_keycut(d, j, bg_prompt="", feedback="", say=_quiet):
    """'장면 안에 서 있는 캐릭터' 한 장을 만든다.

    이 한 장이 Kling 의 입력이 되고, 배경까지 그대로 영상에 실린다.
    """
    d = Path(d)
    n = j.get("keycut_n", 0) + 1
    out = d / ("keycut%d.png" % n)

    say(msg="장면 안에 캐릭터를 그리는 중")
    photo = (d / j["bg_photo"]) if j.get("bg_photo") else None
    pl.keycut(str(d / j["character"]), bg_prompt, str(out),
              place_photo=(str(photo) if photo else None),
              style_mode=j.get("style_mode", "3d"),
              feedback=feedback)
    j["bg_prompt"] = bg_prompt
    j["bg_feedback"] = (feedback or "").strip()
    j["keycut"] = out.name
    j["keycut_n"] = n
    j["keycut_approved"] = False
    j["spent"] = round(j.get("spent", 0) + pl.NB_PRICE, 3)
    if j.get("title") == "새 영상":
        j["title"] = (bg_prompt.strip() or "사진 배경")[:24]
    return j


# ── 소리 · 포스터 · 엔딩 ───────────────────────────────────────────────

def audio_for(d, job):
    """실제로 깔 소리를 고른다."""
    d = Path(d)
    src = job.get("music_src", "reference")
    if src == "none":
        return None
    if src == "file" and job.get("music"):
        return d / job["music"]
    if job.get("ref_audio"):
        return d / job["ref_audio"]
    return (d / job["music"]) if job.get("music") else None


def poster_design(j):
    return dict(accent=j.get("poster_accent"), bg=j.get("poster_bg"), ink=j.get("poster_ink"),
                font=pl.font_file(j.get("poster_font")), bar=j.get("poster_bar", True))


def poster_custom(d, j):
    """포스터는 앱 안에서 만든 것만 쓴다.

    직접 올린 파일을 심었더니 그 안의 인물 사진 때문에 생성 모델이 장면을
    거부했다. 만드는 쪽으로 통일하면 이 실패가 없어지고 디자인도 일관된다.
    """
    return None


def make_still(d, j, say=_quiet):
    """엔딩 장면 그림 한 장을 만든다."""
    d = Path(d)
    say(msg="엔딩 장면 그리는 중")
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
    bake_poster(d, j)
    # 돌려주는 것은 언제나 작업 기록이다. 파일 경로를 돌려주면 그대로 기록
    # 자리에 들어가 저장할 때 터진다 (화면에 PosixPath ... 오류로 떴다).
    return j


def bake_poster(d, j):
    """포스터를 심은 장면 그림. 실패해도 장면 자체는 살려둔다."""
    d = Path(d)
    if j.get("poster_mode", "bake") != "bake" or not j.get("ending_still"):
        return None
    try:
        out = d / "poster_baked.png"
        pl.bake_poster(d / j["ending_still"], j.get("poster") or ["", "", ""], out,
                       custom=poster_custom(d, j), **poster_design(j))
        j["ending_baked"] = out.name
        return out
    except Exception as e:
        j["ending_baked"] = None
        print("포스터 심기 실패:", e)
        return None


def build_ending(d, j, say=_quiet):
    """엔딩 영상을 만든다. 세 군데서 같은 것을 쓰도록 한 곳에 모았다.

    심는 방식이면 포스터를 넣은 그림으로 영상을 만들고, 그 뒤에 글자를 또 얹지 않는다.
    (얹으면 두 번 들어가고, 원본 분홍 그림을 쓰면 분홍이 그대로 남는다)
    """
    import shutil as _sh
    d = Path(d)
    if not j.get("ending_still"):
        make_still(d, j, say)
    baked = bake_poster(d, j) if j.get("poster_mode", "bake") == "bake" else None
    src = baked or (d / j["ending_still"])

    say(msg="엔딩 영상 만드는 중")
    pl.poster_video(str(src), str(d / "ending_raw.mp4"),
                    sec=j.get("ending_sec", 4), baked=bool(baked))
    j["ending_raw"] = "ending_raw.mp4"
    j["spent"] = round(j.get("spent", 0) + pl.POSTER_VID_PRICE, 3)

    if baked:
        _sh.copy(str(d / "ending_raw.mp4"), str(d / "ending.mp4"))
    else:
        say(msg="포스터 글자 얹는 중")
        pl.poster_text(d / "ending_raw.mp4", j.get("poster") or ["", "", ""],
                       d / "ending.mp4", **poster_design(j),
                       progress=lambda i, n: say(done=i, total=n))
    j["ending"] = "ending.mp4"
    return j          # 만든 파일 이름은 j["ending"] 에 들어 있다


def apply_subs(d, j, say=_quiet):
    """원본(final_raw)에 카메라 무빙과 자막을 입혀 최종본을 만든다.

    생성이 아니라 후처리라서 몇 번이든 무료로 다시 할 수 있다.
    카메라를 먼저 걸고 자막을 나중에 얹어야 자막이 같이 흔들리지 않는다.
    """
    import shutil as _sh
    d = Path(d)
    raw = d / "final_raw.mp4"
    cam = d / "final_cam.mp4"
    pl.camera_move(raw, cam, j.get("camera", "normal"))
    if j.get("subs_on") and j.get("subs"):
        say(msg="자막 굽는 중")
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
    return j


def finish_video(d, j, say=_quiet):
    """컷들과 엔딩을 이어 붙이고 자막까지 구워 최종본을 만든다."""
    d = Path(d)
    say(msg="영상 합치는 중", done=0, total=0)
    ending = j.get("ending")
    pl.finish([d / c for c in j["cuts"]], (d / ending) if ending else None,
              audio_for(d, j), d / "final_raw.mp4")
    return apply_subs(d, j, say)


def make_lyrics(d, j, say=_quiet):
    """음원에서 가사 초안을 받아쓴다.

    노래는 자주 틀리므로 사람이 고치는 것을 전제로 한다.
    """
    say(msg="가사를 받아쓰는 중")
    j["subs"] = pl.transcribe(audio_for(d, j))
    j["subs_on"] = True
    j["spent"] = round(j.get("spent", 0) + pl.STT_PRICE, 3)
    return j


def apply_poster_text(d, j, say=_quiet):
    """이미 만든 엔딩 영상에 포스터 글자만 다시 얹는다 (생성 아님 = 무료)."""
    d = Path(d)
    say(msg="포스터 글자 얹는 중")
    pl.poster_text(d / j["ending_raw"], j["poster"], d / "ending.mp4",
                   custom=poster_custom(d, j), **poster_design(j),
                   progress=lambda i, n: say(done=i, total=n))
    j["ending"] = "ending.mp4"
    return j


def finish_up(d, j, want_ending=True, say=_quiet):
    """춤 영상이 다 나온 뒤의 마무리. 엔딩을 붙이고 합치고 자막을 굽는다.

    이미 확인한 엔딩이 있으면 다시 뽑지 않는다 (돈이 든다).
    """
    d = Path(d)
    if want_ending:
        if not (j.get("ending") and (d / j["ending"]).exists()):
            build_ending(d, j, say)
    else:
        j["ending"] = None
    return finish_video(d, j, say)


def poster_preview(d, j, say=_quiet):
    """지금 설정으로 포스터를 한 장 그린다. 생성 모델을 안 쓰므로 공짜고 빠르다."""
    d = Path(d)
    pl.poster_image(j.get("poster") or ["", "", ""], d / "poster_art.png", **poster_design(j))
    return j


def font_list(d=None, j=None, say=_quiet):
    """화면이 고를 수 있는 글꼴과 선택지 목록. 서버가 가진 것을 그대로 알려준다."""
    return {
        "fonts": pl.system_fonts(),
        "poster_kinds": [{"id": k, "label": v["label"]} for k, v in pl.POSTER_KINDS.items()],
        "ending_poses": [{"id": k, "label": v["label"]} for k, v in pl.ENDING_POSES.items()],
        "styles": [{"id": k, "label": v["label"]} for k, v in pl.SUB_STYLES.items()],
        "cameras": [{"id": k, "label": v["label"]} for k, v in pl.CAMERA.items()],
        "style_modes": [{"id": k, "label": v["label"], "note": v["note"]}
                        for k, v in pl.STYLE_MODES.items()],
    }
