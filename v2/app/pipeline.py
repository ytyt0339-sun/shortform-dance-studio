# -*- coding: utf-8 -*-
"""캐릭터 댄스 숏폼 파이프라인.

측정으로 확정한 규칙만 모아둔다. 각 항목 옆의 설명이 그 근거다.

  1) 캐릭터를 잘라내 배경에 얹지 않는다. 배경 프롬프트와 함께 한 장으로 그려서
     Kling 에 넘긴다. Kling 은 image_url 의 배경을 그대로 가져간다.
     (잘라내 합성하면 빛 방향과 접지가 안 맞아 얹은 티가 난다)
  2) 발밑 여백을 반드시 남긴다. 여백 7% 로 넣었더니 다리가 뭉개졌고,
     26% 에서는 30초 내내 멀쩡했다. 캐릭터 크기는 글로 시키지 않는다.
     "화면의 45%" 라고 적어도 세 번 다 다르게(70%, 73%, 33%) 나왔다.
     원하는 크기로 미리 배치한 판(layout_plate)을 주면 그대로 따라 그린다.
  3) 손에 물건을 들리지 않는다. 폼폼을 들렸더니 동작 중에 머리 위로 올라가
     토끼 귀처럼 됐다. 30초 단일 생성에서도 6초 분할에서도 똑같이 나왔다.
  4) 한 번에 길게 뽑지 않는다. 30초 단일 생성은 24초쯤 캐릭터가 무너진다.
     6초씩 끊으면 매번 캐릭터 이미지에서 새로 시작해 형체가 복구된다.
  5) 엔딩 문구는 이미지 모델에 맡기지 않는다. 빈 포스터를 만들고 글자는
     원근 변환으로 얹는다. 한글이 안 깨지고, 문구를 바꿔도 재생성이 없다.
"""
import os
import subprocess
import tempfile
import shutil
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont, ImageFilter

import video as vd

CUT_SEC = 6                     # 4)
NB_EDIT = "fal-ai/nano-banana-2/edit"
NB_MAKE = "fal-ai/nano-banana-2"      # 넣을 그림이 없을 때 (글만으로 만들기)
NB_PRICE = 0.08
# 엔딩 영상 값. fal 은 크기와 길이로 매긴다 — 1000토큰당 $0.0112 이고
# 토큰은 (가로 x 세로 x 초 x 24) / 1024 다. 그래서 상수 하나로 두지 않고
# 실제로 뽑는 크기·길이로 계산한다.
POSTER_VID_RES = "720p"              # 춤 영상(720x1280)과 같은 크기
POSTER_VID_WH = (720, 1280)
_FAL_TOKEN_USD = 0.0112 / 1000


def poster_vid_price(sec=4):
    """엔딩 영상 한 편 값(달러). 480p 로 뽑던 때는 4초에 약 $0.45 였다."""
    w, h = POSTER_VID_WH
    return round(w * h * int(sec) * 24 / 1024 * _FAL_TOKEN_USD, 3)


POSTER_VID_PRICE = poster_vid_price(4)      # 4초 기준 (약 $0.97)

def _default_font(bold):
    """포스터 글자에 쓸 기본 한글 글꼴.

    윈도우에서는 맑은 고딕, 서버(리눅스 컨테이너)에서는 함께 받아둔 글꼴이나
    나눔고딕을 쓴다. 하나도 없으면 None 을 돌려주고, 부르는 쪽에서 PIL 기본
    글꼴로 떨어진다 (한글이 깨지므로 배포 시에는 글꼴을 꼭 넣는다).
    """
    here = Path(__file__).parent
    names = (["malgunbd.ttf", "NanumGothicBold.ttf", "NotoSansKR-Bold.ttf"] if bold
             else ["malgun.ttf", "NanumGothic.ttf", "NotoSansKR-Regular.ttf"])
    roots = ["C:/Windows/Fonts", str(here / "fonts"),
             "/usr/share/fonts/truetype/nanum", "/usr/share/fonts"]
    for r in roots:
        for n in names:
            p = os.path.join(r, n)
            if os.path.exists(p):
                return p
    # 이름이 정확히 안 맞을 때. 전에는 알파벳 순 **첫 파일**을 그냥 집었고,
    # 그래서 서버의 기본 글꼴이 GothicA1-Black 이 되어 있었다 (사용자가 그
    # 글꼴을 골라도 아무 변화가 없어 "글꼴이 안 바뀐다" 로 보였다).
    # 이제 선호 순서를 두고, 굵기도 원하는 쪽을 고른다.
    prefer = ["notosanskr", "nanumgothic", "notoserifkr", "malgun", "nanum", "noto", "gothic"]
    cands = []
    for r in roots:
        d = Path(r)
        if d.is_dir():
            for p in d.rglob("*.[to]tf"):
                if any(k in p.name.lower() for k in ("nanum", "noto", "gothic", "malgun")):
                    cands.append(p)
    if not cands:
        return None

    def rank(p):
        low = p.name.lower()
        fam = next((i for i, k in enumerate(prefer) if k in low), len(prefer))
        if bold:
            w = 0 if "-bold" in low else (1 if "bold" in low else 2)
        else:
            w = 0 if ("regular" in low or "-r." in low) else (1 if "light" in low else 2)
        return (fam, w, len(p.name), str(p))

    return str(min(cands, key=rank))


FONT_B = _default_font(True)
FONT_R = _default_font(False)
ACCENT = (232, 178, 46)
INK = (38, 34, 28)

# 2) 발밑 여백 — 이게 없으면 Kling 이 다리를 뭉갠다
# 크기는 두 방향으로 밀린다. 크면 영상 중간에 더 커져서 팔다리가 잘리고
# (28초 한 번에 뽑았더니 머리 폭 38% -> 52%), 작으면 Kling 이 몸통을 못 찾아
# 생성을 거부한다 (23% 에서 "No complete upper body detected"). 58% 가 그 사이다.
FRAMING_T = (
    "IMPORTANT FRAMING: the character stands in a scene, seen from a little distance. "
    "Its whole body from head to feet spans about %d percent of the image height, and its feet "
    "rest at roughly 80 percent down from the top edge, so there is open background above its "
    "head and a wide stretch of empty ground below its feet. "
    "The character is centered horizontally and clearly narrower than the frame, with open "
    "background on both sides - it must never touch any edge of the image. "
    "Full body visible from the top of the head to the feet, nothing cropped. "
    "Vertical 9:16 composition. No text, no logos, no other characters, no people, "
    "and no extra objects or props lying around. "
    "Drawing it smaller must NOT change the character itself: its colors, markings, face and "
    "clothing stay exactly as in the reference image - same fur color, same everything. ")


def framing(ratio=0.62):
    """캐릭터가 차지할 비율을 글로도 한 번 더 못박는다.

    글의 숫자가 판(layout_plate)과 어긋나면 모델이 둘 사이에서 갈팡질팡한다.
    그래서 판에 쓴 비율을 그대로 받아 쓴다.
    """
    return FRAMING_T % int(round(ratio * 100))

# 3) 손에 든 물건 금지
EMPTY_HANDS = (
    "IMPORTANT: the character's hands are completely EMPTY - it holds nothing at all, no props, "
    "no objects, no pom-poms, no flags. Any accessory must be worn on the body, never held. ")

STYLE = (
    "Render the character as a physical 3D figure: soft matte vinyl toy material with real volume, "
    "rounded edges, subtle subsurface scattering and soft specular highlights. Keep its identity - "
    "shape, colors, face, markings and worn accessories - exactly as in the reference image. ")

LIGHT = (
    "Place it inside the scene with photographic lighting: clear directional sunlight casting a "
    "soft realistic contact shadow on the ground under its feet, shallow depth of field with the "
    "background gently blurred, cinematic 3D animation film look. ")


# 손그림 모드. 그린 사람의 삐뚤빼뚤한 맛을 살리되, 몸통과 팔다리는 또렷하게 만든다.
# (모션 전이는 팔다리를 못 찾으면 캐릭터를 뭉개므로 이 부분은 양보하지 않는다)
DOODLE_STYLE = (
    "Redraw the character as a hand-drawn crayon-and-marker doodle on paper: wobbly uneven "
    "outlines of varying thickness, flat naive coloring that strays a little outside the lines, "
    "visible paper grain, no gradients and no 3D shading. Keep the original drawing's identity - "
    "its shapes, colors, face and proportions - and keep its charming clumsiness. "
    "IMPORTANT: the head, torso, both arms and both legs must each be clearly separated and "
    "easy to tell apart, with arms held away from the body. ")

DOODLE_LIGHT = (
    "Draw the whole scene in the same crayon doodle style on the same sheet of paper: simple "
    "wobbly shapes, flat colors, a loosely scribbled ground line under the feet and a small "
    "hand-drawn shadow. No photographic texture, no realistic lighting, no depth of field. ")

REAL_STYLE = (
    "Render the character as a REAL physical thing captured by a camera: a life-size mascot "
    "costume or plush made of actual fabric, with visible fibres, stitched seams, slight "
    "sagging where the material folds and tiny natural imperfections. Keep its identity - "
    "shape, colors, face, markings and worn accessories - exactly as in the reference image. "
    "True-to-life materials and micro-texture. Not an illustration, not cartoon shading, not "
    "a glossy CG render - it must read as a photograph of something that physically exists. ")

REAL_LIGHT = (
    "Photograph it on location in real daylight: natural exposure and white balance, a soft "
    "contact shadow on the ground under its feet, shallow depth of field with the background "
    "gently blurred, and fine photographic grain. It should look like a frame shot on a "
    "full-frame camera, not a render. ")

STYLE_MODES = {
    "3d": dict(label="3D 피규어", style=None, light=None,
               note="말랑한 장난감 질감 + 사진 같은 배경"),
    "doodle": dict(label="손그림 낙서체", style=DOODLE_STYLE, light=DOODLE_LIGHT,
                   note="크레파스 낙서 느낌 + 종이 위에 그린 배경"),
    "real": dict(label="실사", style=REAL_STYLE, light=REAL_LIGHT,
                 note="진짜 탈인형 같은 천 질감 + 실제로 찍은 듯한 배경"),
}


def mode_of(name):
    """화풍 모드의 (그림체, 조명) 문장. 기본은 3D 피규어."""
    m = STYLE_MODES.get(name) or STYLE_MODES["3d"]
    return (m["style"] or STYLE), (m["light"] or LIGHT)


# 고른 화풍으로 캐릭터를 다시 그리는 단계. 누가 봐도 같은 캐릭터인데 그림체만
# 바뀐다. 화풍 문장(mode_of)은 이 뒤에 붙는다.
#
# 전에는 "원본 낙서를 그대로 두라"고만 적어서, 손그림을 골라도 낙서체로 바뀌지
# 않았다 (이미 낙서인 그림만 정리될 뿐이었다).
CLEAN_REDRAW = (
    "Redraw the character from the reference image as one clean, complete character on a plain "
    "white background. Keep its identity so it is unmistakably the same character: the same "
    "shapes, colours, face, markings, clothing and worn accessories, and the same proportions. "
    "What must change is only HOW it is drawn - follow the style instruction below exactly, "
    "even when the reference image is drawn in a completely different style. "
    "IMPORTANT: give it a clearly readable body - head, torso, two arms and two legs each "
    "distinct and separated, arms held away from the body in a relaxed A-pose, feet flat, "
    "nothing crossed and no props held in the hands. "
    "Full body from head to feet, centred, with a generous empty margin on all sides. "
    "No text, no background, no other characters. ")


# 앉아 있거나 얼굴만 있는 그림은 춤 동작이 어색하게 나온다. 춤 모델에 넣기
# 전에 같은 캐릭터를 서 있는 전신으로 다시 세워 둔다.
STAND_POSE = (
    "Redraw the SAME character from the reference image standing upright, seen from the front, "
    "full body from the top of the head to the soles of both feet, on a plain light background. "
    "Keep its identity EXACTLY: the same shapes, colours, face, markings, clothing and worn "
    "accessories. Do not change its proportions and do not turn it into a different character. "
    "How it is drawn follows the style instruction below - that part may change. "
    "If the reference shows it sitting, lying down, crouching, cropped or only from the chest up, "
    "reconstruct the missing parts so they match what is visible, and stand it up. "
    "IMPORTANT: head, torso, two arms and two legs must each be distinct and separated - arms "
    "held away from the body in a relaxed A-pose, legs slightly apart, both feet flat on the "
    "ground, nothing crossed, nothing hidden behind the body and no props held in the hands. "
    "The whole figure is centred with a generous empty margin on every side, and no part of it "
    "touches or runs past the edge of the frame. "
    "No text, no furniture, no other characters, no busy background. ")


# 캐릭터가 아예 없는 사람을 위한 길. 글로 적거나 사람 사진을 주면 만들어 준다.
CHAR_BODY = (
    "Full body from the top of the head to the soles of both feet, standing upright and facing "
    "the camera in a relaxed A-pose: arms held away from the body, legs slightly apart, both "
    "feet flat on the ground, nothing held in the hands. Head, torso, both arms and both legs "
    "must each be distinct and easy to tell apart. One character only, centred on a plain light "
    "background with a generous empty margin on every side, no part touching the frame edge. "
    "No text, no furniture, no scenery, no other characters. ")

CHAR_FROM_TEXT = (
    "Design an original friendly mascot character from this description: %s. ")

CHAR_FROM_PHOTO = (
    "The reference image is a photograph of a real person. Design an original mascot character "
    "inspired by THAT person. Carry over what makes them recognisable at a glance - hair colour "
    "and hairstyle, skin tone, glasses, facial hair, and the colours and shapes of the clothes "
    "they are wearing - and simplify the rest into a friendly character. "
    "Do not reproduce the face photographically and do not copy anything from the photo's "
    "background. The result is a stylised character, not a portrait. ")


def make_character(out_path, prompt="", person_photo=None, style_mode="3d"):
    """캐릭터를 새로 만든다. 사람 사진을 주면 그 특징을 옮겨 담는다."""
    tmp = tempfile.mkdtemp()
    try:
        st, _ = mode_of(style_mode)
        want = (prompt or "").strip().rstrip(".")
        if person_photo:
            body = CHAR_FROM_PHOTO
            if want:
                body += "Also follow this request: %s. " % want
            imgs = [person_photo]
        else:
            body = CHAR_FROM_TEXT % (want or "a cheerful animal mascot")
            imgs = []
        return _nb(body + CHAR_BODY + st, imgs, out_path, tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def stand_character(char_path, out_path, style_mode="3d", feedback=None):
    """캐릭터를 서 있는 전신 한 장으로 다시 세운다. 원본은 건드리지 않는다."""
    tmp = tempfile.mkdtemp()
    try:
        st, _ = mode_of(style_mode)
        return _nb(STAND_POSE + st + fix_note(feedback), [char_path], out_path, tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def fix_note(feedback):
    """사용자가 적어준 수정 요청을 프롬프트 조각으로 만든다.

    맨 뒤에 붙인다. 앞쪽 지시와 부딪히면 이쪽이 이겨야 다시 만든 보람이 있다.
    한국어로 적어도 그대로 넘긴다 (모델이 알아듣는다).
    """
    t = (feedback or "").strip().rstrip(".")
    if not t:
        return ""
    return ("MOST IMPORTANT - the previous attempt was rejected by the user. "
            "Apply this correction and make it clearly visible in the new image: %s. " % t)


def clean_sketch(char_path, out_path, style_mode="3d", feedback=None):
    """캐릭터를 고른 화풍으로 다시 그린다. 원본을 덮어쓰지 않고 새 파일로 만든다."""
    tmp = tempfile.mkdtemp()
    try:
        st, _ = mode_of(style_mode)
        return _nb(CLEAN_REDRAW + st + fix_note(feedback), [char_path], out_path, tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def retry(fn, tries=3, wait=4):
    """fal 호출은 DNS·연결이 잠깐 끊겨도 실패한다. 몇 번 다시 시도한다."""
    import time as _t
    last = None
    for i in range(tries):
        try:
            return fn()
        except Exception as e:
            last = e
            s = str(e)
            transient = ("getaddrinfo" in s or "ConnectError" in s or "Timeout" in s
                         or "Connection" in s or "502" in s or "503" in s or "504" in s)
            if not transient or i == tries - 1:
                raise
            _t.sleep(wait * (i + 1))
    raise last


def _fal():
    import fal_client
    if not os.environ.get("FAL_KEY"):
        raise RuntimeError("FAL_KEY가 설정되지 않았습니다.")
    return fal_client


def _ascii_copy(src, tmp, name):
    """fal 업로더가 한글 파일명을 못 받는다. ASCII 이름으로 복사해서 올린다."""
    p = Path(tmp) / name
    p.write_bytes(Path(src).read_bytes())
    return str(p)


def _nb(prompt, images, out_path, tmp):
    """넣을 그림이 있으면 고치는 모델로, 없으면 글만으로 만드는 모델로 간다."""
    fal = _fal()
    args = {"prompt": prompt, "aspect_ratio": "9:16",
            "resolution": "2K", "output_format": "png"}
    if images:
        args["image_urls"] = [fal.upload_file(_ascii_copy(p, tmp, "in%d%s" % (i, Path(p).suffix)))
                              for i, p in enumerate(images)]
    r = retry(lambda: fal.subscribe(NB_EDIT if images else NB_MAKE,
                                    arguments=args, with_logs=False))
    urllib.request.urlretrieve(r["images"][0]["url"], out_path)
    return out_path


def _trim(im, tol=18):
    """그림에서 캐릭터가 차지하는 네모를 찾는다. 종이 결 때문에 완전 흰색이 아니라
    모서리 색을 배경으로 보고 그보다 진한 곳만 캐릭터로 센다."""
    a = np.asarray(im.convert("RGB")).astype(np.int16)
    h, w = a.shape[:2]
    corners = np.array([a[0, 0], a[0, w - 1], a[h - 1, 0], a[h - 1, w - 1]])
    bg = corners.mean(axis=0)
    d = np.abs(a - bg).max(axis=2)
    ys, xs = np.where(d > tol)
    if len(xs) < 50:
        return (0, 0, w, h)
    return (int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1)


def layout_plate(char_path, out_path, ratio=0.62, feet=0.80, size=(1080, 1920),
                 wide=0.80, floor=0.40):
    """캐릭터를 정해진 크기로 9:16 판 위에 올려두고, **실제로 올린 비율**을 돌려준다.

    크기를 글로 시키면 매번 다르게 나온다 (45% 로 적어도 70% 가 나왔다).
    아예 원하는 크기로 배치한 판을 주고 '그대로 두고 배경만 그려라' 라고 하면
    모델이 배치를 따라간다. ratio 는 캐릭터 키가 차지할 비율, feet 는 발바닥 위치.

    wide 는 가로로 퍼진 캐릭터가 넘지 않을 폭이다. 갈기가 큰 사자처럼 옆으로
    넓은 그림은 여기 먼저 걸려서, 키를 아무리 키워도 같은 크기로 눌렸다
    (보통 58.8%, 크게 58.8% — 골라도 안 바뀐다는 말이 여기서 나왔다).
    그래서 wide 도 크기 선택에 따라 같이 움직인다.

    floor 아래로는 안 내린다. 45% 판에서 모델이 23% 까지 줄여 그렸을 때
    Kling 이 "몸통을 못 찾겠다"며 거부한 적이 있다. 지금은 판을 거의 그대로
    따르므로(49.9% 판 → 51.2% 그림) 그때만큼 위험하지는 않다.
    """
    W, H = size
    im = Image.open(char_path)
    if im.mode in ("RGBA", "LA", "P"):
        im = im.convert("RGBA")
        flat = Image.new("RGB", im.size, (255, 255, 255))
        flat.paste(im, mask=im.split()[-1])
        im = flat
    else:
        im = im.convert("RGB")
    box = _trim(im)
    cut = im.crop(box)
    hh = int(H * ratio)
    ww = max(1, round(cut.width * hh / cut.height))
    # 가로로 퍼진 캐릭터는 폭에 먼저 걸린다. 그만큼 키를 줄인다.
    if ww > W * wide:
        ww = int(W * wide)
        hh = max(1, round(cut.height * ww / cut.width))
    # 다만 폭 때문에 줄인 키가 아래 한계를 넘어가면, 한계를 지킨다.
    # (너무 작으면 Kling 이 몸통을 못 찾아 생성 자체를 거부한다)
    if hh < H * floor:
        hh = int(H * floor)
        ww = max(1, round(cut.width * hh / cut.height))
    cut = cut.resize((ww, hh), Image.LANCZOS)
    plate = Image.new("RGB", (W, H), (255, 255, 255))
    plate.paste(cut, ((W - ww) // 2, int(H * feet) - hh))
    plate.save(out_path)
    return out_path, hh / H          # 글에도 **실제로 올린** 비율을 써야 한다


# 캐릭터 크기. 글로 "작게 그려줘" 라고 적어도 안 먹는다 — 크기는 아래
# layout_plate 가 판에 **직접** 박아 넣고, LAYOUT_LOCK 이 "그 크기를 그대로
# 지켜라" 라고 못박기 때문이다. 그래서 수정사항이 아니라 값으로 받는다.
#
# 폭이 좁은 데는 이유가 있다. 크면 영상 중간에 더 커져서 팔다리가 잘리고,
# 작으면 Kling 이 몸통을 못 찾아 생성 자체를 거부한다. 이 파일 위쪽에 적어
# 둔 대로 58% 가 아래쪽 한계다.
#
# 한 번 0.52 로 열었다가 생성이 실패했다 (fal 이 422 로 돌려줬다). 다시는
# 58% 아래로 내리지 않는다.
# (키 비율, 폭 한계). 둘을 같이 움직여야 고른 값이 실제로 달라진다.
#
# 키만 바꾸면 가로로 퍼진 캐릭터는 폭 한계(80%)에 먼저 걸려 보통과 크게가
# 똑같아진다. 갈기 큰 사자로 재보니 58.0 / 58.8 / 58.8% 였다 — 골라도 안
# 바뀐다는 말이 여기서 나왔다.
#
# 보통(0.62/0.80)은 지금까지 멀쩡히 돌아간 값이라 손대지 않는다.
#
# '작게'(49.9%) 도 보통(58.8%) 과 5%p 차이뿐이라 눈에 안 들어온다는 말이 있어
# 한 칸 더 뒀다. 모델이 판을 얼마나 잘 따르는지 실제로 재보고 내린 결정이다:
# 판 49.9% → 나온 그림 51.2% (1.3%p 차이). 예전에 Kling 이 거부한 건 23% 였고,
# 그건 모델이 제멋대로 줄여 그리던 시절 이야기다.
KEY_SIZES = {
    "xs":    (0.45, 0.58),
    "small": (0.52, 0.68),
    "mid":   (0.62, 0.80),
    "big":   (0.72, 0.90),
}


LAYOUT_LOCK = (
    "The FIRST image is a layout guide: the character is already placed at exactly the size and "
    "position it must keep. Keep its size, its position on the canvas and its proportions "
    "EXACTLY as given - do not enlarge it, do not move it, do not zoom in, do not crop it. "
    "Only two things change: the empty white area around it becomes the scene, and the "
    "character is re-rendered in the style described below while staying the same size. "
    "The scene must fill the WHOLE canvas edge to edge - sky or background all the way up to "
    "the top edge, ground all the way down to the bottom edge and out to both sides. "
    "Leave no blank paper, no white border, no vignette: every corner is part of the scene. "
    "Do not shrink the character further than the layout shows - its head and torso must be "
    "big enough to read clearly. ")


DEFAULT_POSE = ("Pose: standing upright facing the camera in a relaxed ready stance, arms "
                "slightly away from the body, feet flat on the ground. ")


# 장소 사진을 주면 그 장소를 그대로 옮겨 그린다. 사진을 일러스트로 바꾸는 셈이라
# 실제 캠퍼스·건물처럼 알아볼 수 있는 배경이 나온다.
# 엔딩 배경을 따로 안 고른 경우. "비슷한 곳" 으로는 부족하다 — 2번 배경과
# 미묘하게 다른 장소가 나온다는 말이 있었다. **같은 장면의 1초 뒤**라고
# 못박고, 바꿔도 되는 것을 딱 하나(표지판)로 못 박는다.
BG_KEEP = (
    "IMPORTANT - SAME PLACE, SAME MOMENT: the FIRST reference image already shows this character "
    "standing in its scene. This new picture is the SAME shot a second later, taken from the same "
    "spot with the same lens. Copy that background exactly: the same buildings with the same "
    "shapes, windows and colours, the same ground surface and its pattern, the same plants, the "
    "same sky and clouds, the same weather, the same time of day, the same direction of light and "
    "the same depth of field. Do not move to another spot, do not change the viewpoint, the "
    "distance or the horizon, do not redesign or redecorate anything, and do not invent objects "
    "that are not already there. The ONLY change is that a sign now stands in this scene, and the "
    "framing shifts just enough to fit it in. ")

PLACE_REF = (
    "The SECOND reference image is a photo the user chose for the setting. First look at it and "
    "decide what it actually shows. "
    "IF IT IS A REAL PLACE - a campus, a street, a room, a stage, a landscape - recreate THAT "
    "location as the setting: keep its recognizable layout, buildings, landmarks, materials, "
    "colors and time of day, seen from a similar viewpoint. "
    "IF IT IS NOT A PLACE - an object, a pattern, a texture, a fabric, an artwork, a poster, a "
    "close-up, a single item or just a mood shot - do NOT try to read it as a location and do "
    "NOT paste it in flat. Instead build a believable setting that is clearly inspired by it: "
    "carry over its colors, materials, textures, motifs, mood and lighting, and invent "
    "surroundings that would plausibly hold that thing. "
    "In both cases: do not copy any people, text or signage from the photo, and render the "
    "setting in the same style as the character, not as a raw photograph. ")


def keycut(char_path, bg_prompt, out_path, pose=None, place_photo=None, style_mode="3d",
           feedback=None, size="mid"):
    """1) 캐릭터를 배경 안에 세운 한 장. 이게 Kling 의 입력이 된다.

    place_photo 를 주면 그 장소를 재현한다. 글로만 적는 것보다 훨씬 정확하다.
    """
    txt = (bg_prompt or "").strip().rstrip(".")
    scene = ("Setting: %s. " % txt) if txt else ""
    tmp = tempfile.mkdtemp()
    try:
        st, li = mode_of(style_mode)
        ratio, wide = KEY_SIZES.get(size) or KEY_SIZES["mid"]
        # 판이 실제로 올린 비율을 받아 글에도 같은 숫자를 쓴다. 둘이 어긋나면
        # 모델이 그 사이에서 갈팡질팡한다.
        plate, got = layout_plate(char_path, os.path.join(tmp, "plate.png"),
                                  ratio=ratio, wide=wide)
        imgs = [plate] + ([place_photo] if place_photo else [])
        prompt = LAYOUT_LOCK + (pose or DEFAULT_POSE) + st + EMPTY_HANDS + scene
        if place_photo:
            prompt += PLACE_REF
        return _nb(prompt + li + framing(got) + fix_note(feedback), imgs, out_path, tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def split_plate(plate, out_dir, sec=CUT_SEC):
    """4) 레퍼런스를 6초씩 자른다."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    dur = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                                "-of", "csv=p=0", str(plate)],
                               capture_output=True, text=True).stdout.strip())
    segs = []
    for i in range(max(1, int(dur // sec))):
        p = out_dir / ("seg%d.mp4" % (i + 1))
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", str(i * sec), "-t", str(sec),
                        "-i", str(plate), "-c", "copy", str(p)], check=True)
        segs.append(p)
    return segs


def kling_cuts(key_path, segs, out_dir, progress=None, prompt=None):
    """각 구간을 같은 캐릭터 이미지로 생성한다. 6초마다 캐릭터가 복구된다."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    tmp = tempfile.mkdtemp()
    done = [0]
    try:
        key = _ascii_copy(key_path, tmp, "key.png")

        def one(pair):
            i, seg = pair
            out = out_dir / ("cut%d.mp4" % (i + 1))
            vd.generate(key, _ascii_copy(seg, tmp, "seg%d.mp4" % (i + 1)), str(out),
                        engine="kling", orientation="video", prompt=prompt)
            done[0] += 1
            if progress:
                progress(done[0], len(segs))
            return out

        with ThreadPoolExecutor(min(5, len(segs))) as ex:
            return list(ex.map(one, enumerate(segs)))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def kling_single(key_path, plate, out_path, progress=None, prompt=None):
    """레퍼런스 전체를 한 번에 뽑는다.

    6초씩 나눠 붙이면 이음매가 눈에 띈다. 한 번에 뽑으면 끊김이 없는 대신
    뒤로 갈수록 캐릭터가 조금씩 변형된다 (발밑 여백이 충분하면 훨씬 덜하다).
    Kling 은 character_orientation="video" 에서 최대 30초까지 받는다.
    """
    tmp = tempfile.mkdtemp()
    try:
        if progress:
            progress(0, 1)
        vd.generate(_ascii_copy(key_path, tmp, "key.png"),
                    _ascii_copy(plate, tmp, "plate.mp4"), str(out_path),
                    engine="kling", orientation="video", prompt=prompt)
        if progress:
            progress(1, 1)
        return Path(out_path)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---- 엔딩 포스터 -------------------------------------------------------------

# 엔딩 장면. 사용자가 고를 수 있는 부분만 조각으로 두고 나머지는 고정한다.
POSTER_KINDS = {
    "wall": dict(label="벽에 붙은 포스터",
                 desc="a large poster mounted flat on a wall beside the character"),
    "board": dict(label="세워둔 안내판",
                  desc="a large freestanding sign board on legs next to the character"),
    "banner": dict(label="세로 배너(현수막)",
                   desc="a tall vertical fabric banner hanging from a pole beside the character"),
    "frame": dict(label="게시판 액자",
                  desc="a big framed notice board mounted on a wall beside the character"),
}
ENDING_POSES = {
    "point": dict(label="포스터를 가리키기",
                  desc="turning toward the camera and pointing one arm up at it"),
    "wave": dict(label="손 흔들기",
                 desc="standing beside it and waving at the camera with a big smile"),
    "open": dict(label="두 팔 벌리기",
                 desc="standing beside it with both arms flung wide open toward the camera"),
    "look": dict(label="올려다보기",
                 desc="standing under it and looking up at it with both hands behind its back"),
}

POSTER_BLANK = (
    "IMPORTANT: the sign, poster or board in the scene is COMPLETELY BLANK and filled with one "
    "flat solid BRIGHT MAGENTA color (hot pink, like #FF00FF) - absolutely no text, no letters, "
    "no writing, no numbers, no logos, no drawings and no shading anywhere on it, just one even "
    "magenta fill. Nothing else in the picture may be magenta or pink. "
    "It is a plain magenta rectangle in portrait orientation, flat and "
    "fully visible with all four corners inside the frame, slightly angled so its perspective "
    "reads naturally, occupying roughly the upper half of the image. The character stands on "
    "the ground in the lower part of the frame, full body from head to feet visible, its feet "
    "at about 85 percent down from the top. Vertical 9:16. No other characters, no people in "
    "the foreground. ")

POSTER_SCENE = (
    "Scene: the character stands outdoors next to %s, %s. "
    "IMPORTANT: the sign is COMPLETELY BLANK and filled with one flat solid BRIGHT MAGENTA "
    "color (hot pink, RGB 255 0 255) - absolutely no text, no letters, no writing, no "
    "numbers, no logos, no drawings and no shading anywhere on it, just one even magenta "
    "fill covering the whole sign. Nothing else in the picture may be magenta or pink. "
    "It is a plain magenta rectangle in portrait orientation, flat and fully visible with "
    "all four corners inside the frame, slightly angled so its perspective reads naturally, "
    "occupying roughly the upper half of the image. "
    "The character stands on the ground in the lower part of the frame, full body from "
    "head to feet visible, its feet at about 85 percent down from the top. "
    "Vertical 9:16. No other characters, no people in the foreground. ")

LOCKED = (
    "LOCKED-OFF STATIC CAMERA - the camera does not move, pan, zoom or shake at all. The sign "
    "stays perfectly still in the exact same place in frame. Only the character moves: it bounces "
    "lightly, gestures toward the sign and smiles at the camera. The sign stays completely blank, "
    "one flat solid bright magenta fill, with no text ever appearing on it.")


def poster_scene(key_path, bg_prompt, out_path, kind="wall", pose="point", scene=None,
                 free=None, place_photo=None, style_mode="3d"):
    """엔딩 장면 한 장. 표지판은 반드시 비워둔다 (글자는 나중에 얹는다).

    free 를 주면 정해진 표지판·자세 대신 그 서술을 쓴다.
    place_photo 를 주면 그 장소를 재현한다.
    """
    tmp = tempfile.mkdtemp()
    try:
        if free and free.strip():
            body = ("Scene: %s. " % free.strip().rstrip(".")) + POSTER_BLANK
        else:
            k = (POSTER_KINDS.get(kind) or POSTER_KINDS["wall"])["desc"]
            ps = (ENDING_POSES.get(pose) or ENDING_POSES["point"])["desc"]
            body = POSTER_SCENE % (k, ps)
        where = (scene or bg_prompt or "").strip().rstrip(".")
        extra = ("The surroundings match this setting: %s. " % where) if where else ""
        # 사진이 없으면 키컷 한 장만 보낸다. 같은 그림을 두 번 보내면 모델이
        # 서로 다른 참고 자료로 보고 둘을 섞으려 든다.
        imgs = [key_path, place_photo] if place_photo else [key_path]
        if place_photo:
            extra += PLACE_REF
        elif not (scene or "").strip():
            # 엔딩 배경을 따로 안 고른 경우. 글로만 맡기면 키컷과 미묘하게 다른
            # 장소가 나와서, 키컷 그림의 배경을 그대로 쓰라고 못박는다.
            extra = BG_KEEP + extra
        st, li = mode_of(style_mode)
        return _nb(body + extra + st + EMPTY_HANDS + li, imgs, out_path, tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


LOCKED_BAKED = (
    "LOCKED-OFF STATIC CAMERA - the camera does not move, pan, zoom or shake at all. "
    "The poster on the wall stays perfectly still in the exact same place, and its printed "
    "design and text must stay EXACTLY as they are in the input image - do not redraw, "
    "restyle, translate, blur or alter a single letter, and do not add or remove any text. "
    "Only the character moves: it bounces lightly, gestures toward the poster and smiles "
    "at the camera.")


def poster_video(still, out_path, sec=4, baked=False):
    """카메라를 고정시켜야 표지판이 안 움직여서 글자를 한 번만 맞추면 된다."""
    fal = _fal()
    tmp = tempfile.mkdtemp()
    try:
        r = retry(lambda: fal.subscribe("bytedance/seedance-2.0/fast/image-to-video", arguments={
            "prompt": LOCKED_BAKED if baked else LOCKED,
            "image_url": fal.upload_file(_ascii_copy(still, tmp, "poster.png")),
            "resolution": POSTER_VID_RES, "duration": str(int(sec)), "generate_audio": False,
        }, with_logs=False))
        urllib.request.urlretrieve(r["video"]["url"], out_path)
        return out_path
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _order(pts):
    """좌상·우상·우하·좌하 순서로 정렬."""
    s_, d_ = pts.sum(1), np.diff(pts, axis=1).ravel()
    return np.array([pts[s_.argmin()], pts[d_.argmin()], pts[s_.argmax()], pts[d_.argmax()]])


def _corners(pts):
    """표지판의 네 꼭짓점.

    캐릭터 손이 표지판을 가리면 영역이 사각형이 아니게 되고, 그때 극점만 쓰면
    가려진 쪽 모서리가 안으로 밀린다. 볼록 껍질에 사각형을 맞춰서 원래 모서리를
    되찾는다.
    """
    try:
        import cv2
        c = pts.astype(np.float32).reshape(-1, 1, 2)
        hull = cv2.convexHull(c)
        peri = cv2.arcLength(hull, True)
        box = _order(np.array(cv2.boxPoints(cv2.minAreaRect(hull)), dtype=float))
        for k in range(1, 40):
            ap = cv2.approxPolyDP(hull, 0.002 * k * peri, True)
            if len(ap) != 4:
                continue
            q = _order(ap.reshape(4, 2).astype(float))
            # 네 점이 표지판을 다 감싸야 한다. 못 감싸면 빈 자리가 검게 남는다.
            inside = np.array([cv2.pointPolygonTest(q.astype(np.float32), (float(x), float(y)),
                                                    False) >= 0
                               for x, y in pts[::max(1, len(pts) // 3000)]])
            if inside.mean() > 0.995:
                return q
            break
        return box          # 다 못 감싸면 감싸는 게 보장된 회전 사각형을 쓴다
    except Exception:
        return _order(pts)


def _outset(q, k=0.018):
    """네 꼭짓점을 가운데에서 바깥으로 조금 밀어낸다.

    딱 맞게 덮으면 표시색(마젠타)이 가장자리에 실오라기처럼 남는다.
    """
    c = q.mean(0)
    return c + (q - c) * (1.0 + k)


def _find_quad(im):
    """포스터의 네 꼭짓점.

    장면을 만들 때 표지판을 마젠타로 칠하게 해두었다. 어떤 화풍이든 그 색 하나만
    찾으면 되고, 어차피 그 위에 포스터를 통째로 덮어 그리므로 색은 화면에 안 남는다.
    (예전에는 '밝고 채도 낮은 최대 영역'으로 찾았는데, 낙서 화풍은 그림 전체가
     흰 종이 위에 있어서 종이 배경이 통째로 걸렸다)
    """
    from scipy import ndimage
    a = np.asarray(im.convert("RGB")).astype(int)
    h, w = a.shape[:2]
    r, g, b = a[..., 0], a[..., 1], a[..., 2]
    area = h * w

    masks = [
        (r > 110) & (b > 110) & (r - g > 45) & (b - g > 30),   # 마젠타
        (a.max(2) > 195) & ((a.max(2) - a.min(2)) < 38),       # 예전 방식 (흰 표지판)
    ]
    for m in masks:
        if m.sum() < area * 0.02:
            continue
        lab, n = ndimage.label(m)
        if n == 0:
            continue
        edge = set(lab[0, :]) | set(lab[-1, :]) | set(lab[:, 0]) | set(lab[:, -1])
        sizes = ndimage.sum(m, lab, range(1, n + 1))
        cands = [(sizes[i - 1], i) for i in range(1, n + 1)
                 if i not in edge and 0.03 * area <= sizes[i - 1] <= 0.75 * area]
        if not cands:
            cands = [(sizes[i - 1], i) for i in range(1, n + 1)
                     if 0.03 * area <= sizes[i - 1] <= 0.60 * area]
        if not cands:
            continue
        k = max(cands)[1]
        ys, xs = np.where(lab == k)
        q = _corners(np.stack([xs, ys], 1).astype(float))
        # 화면을 거의 다 덮는 사각형은 표지판이 아니라 배경이다. 조용히 쓰면
        # 글자가 화면 전체에 비스듬히 깔린다 — 차라리 실패로 알린다.
        bw = q[:, 0].max() - q[:, 0].min()
        bh = q[:, 1].max() - q[:, 1].min()
        if bw > w * 0.92 and bh > h * 0.92:
            continue
        return _outset(q)
    raise RuntimeError(
        "표지판 위치를 찾지 못했습니다. 5단계에서 장면 이미지를 다시 만들어보세요 "
        "(표지판이 분홍색으로 칠해져 나와야 합니다).")


def _hex(c, dflt):
    c = (c or "").lstrip("#")
    if len(c) != 6:
        c = dflt.lstrip("#")
    try:
        return tuple(int(c[i:i + 2], 16) for i in (0, 2, 4))
    except ValueError:
        return tuple(int(dflt.lstrip("#")[i:i + 2], 16) for i in (0, 2, 4))


def _poster_art(w, h, lines, accent=None, bg=None, ink=None, font=None, bar=True):
    """포스터에 들어갈 그림. 정면 기준으로 그리고 나중에 원근을 입힌다."""
    ac = _hex(accent, "#E8B22E")
    bgc = _hex(bg, "#FCFAF5")
    ic = _hex(ink, "#26221C")
    # font 는 **글꼴 이름**이다 (경로가 아니다). 경로만 받으면 가변 글꼴의
    # 굵기를 못 살려서 Light 든 Black 이든 같은 모양으로 그려졌다.
    im = Image.new("RGB", (w, h), bgc)
    d = ImageDraw.Draw(im)
    if bar:
        d.rectangle([0, 0, w, int(h * .16)], fill=ac)
    safe = w * .84

    def wide(txt, f):
        return d.textbbox((0, 0), txt, font=f)[2] - d.textbbox((0, 0), txt, font=f)[0]

    def wrap(txt, f):
        """폭에 맞게 줄을 나눈다. 띄어쓰기로 먼저 끊고, 한 덩어리가 그래도
        넘치면 글자 단위로 끊는다 (한글은 띄어쓰기가 드물어 이 경우가 흔하다)."""
        out, cur = [], ""
        for word in txt.split(" "):
            nxt = (cur + " " + word) if cur else word
            if cur and wide(nxt, f) > safe:
                out.append(cur)
                cur = word
            else:
                cur = nxt
        if cur:
            out.append(cur)
        done = []
        for ln in out:
            while len(ln) > 1 and wide(ln, f) > safe:
                cut = len(ln)
                while cut > 1 and wide(ln[:cut], f) > safe:
                    cut -= 1
                done.append(ln[:cut])
                ln = ln[cut:]
            if ln:
                done.append(ln)
        return done

    def load(bold, size):
        return font_of(font, size, bold=bold), bold

    def fit(bold, txt, size, max_lines):
        """줄바꿈까지 해서 max_lines 안에 들어가는 글꼴과 줄 목록.

        전에는 8px 까지 줄이기만 해서, 제목이 길면 그래도 안 들어가 포스터
        밖으로 삐져나가 잘렸다. 지금은 조금 줄여 한 줄에 들어가면 한 줄로 두고
        (짧은 글의 모양이 전과 같게), 그래도 넘치면 줄을 나눠 담는다."""
        one = max(10, int(size * .72))     # 한 줄로 담으려고 줄일 수 있는 한계
        s = size
        while s >= one:
            f, bold = load(bold, s)
            if len(wrap(txt, f)) <= 1:
                return f, [txt]
            s = int(s * .96)
        floor = max(10, int(size * .5))
        s = size
        while True:
            f, bold = load(bold, s)
            ls = wrap(txt, f)
            # 넘치는 줄도 **버리지 않고** 다 돌려준다. 전에는 ls[:max_lines] 로
            # 잘라내서, 표지판이 세로로 길면 적어둔 글자가 통째로 사라졌다.
            # 다 안 들어가면 바깥에서 글자 기준 크기를 줄여 다시 짠다.
            if len(ls) <= max_lines or s <= floor:
                return f, ls
            s = int(s * .92)

    def block(ls, f, top, fill):
        """가운데 정렬로 줄을 쌓고, 다음 줄이 시작될 y 를 돌려준다."""
        y = top
        for ln in ls:
            bb = d.textbbox((0, 0), ln, font=f)
            d.text(((w - (bb[2] - bb[0])) / 2 - bb[0], y), ln, font=f, fill=fill)
            y += int(f.size * 1.22)
        return y

    l1, l2, l3 = (list(lines) + ["", "", ""])[:3]

    def plan(base):
        """글자 기준 크기 base 로 짰을 때의 배치. 다 못 담으면 fits=False."""
        f1, ls1 = fit(True, l1, int(base * .095), 2) if l1 else (None, [])
        f2, ls2 = fit(True, l2, int(base * .135), 2) if l2 else (None, [])
        f3, ls3 = fit(False, l3, int(base * .065), 2) if l3 else (None, [])
        lh = lambda f, ls: len(ls) * int(f.size * 1.22) if ls else 0
        h1, h2, h3 = lh(f1, ls1), lh(f2, ls2), lh(f3, ls3)
        gap = int(base * .05) if (ls1 and ls2) else 0
        # 위 블록(제목·강조줄)은 0.28h~0.61h 안에서 세로 가운데에 놓는다.
        top0 = int(h * .28) + max(0, (int(h * .33) - (h1 + gap + h2)) // 2)
        after = top0 + h1 + (gap if ls1 else 0) + h2
        # 줄이 늘어나면 글자가 구분선을 뚫고 나간다. 선을 글자 아래로 밀어 둔다.
        div = max(int(h * .63), after + int(h * .025))
        y3 = div + int(h * .03)
        room = h - int(h * .06) - y3
        fits = (top0 >= int(h * .17)) and (y3 + h3 <= h - int(h * .04))
        return dict(f1=f1, ls1=ls1, f2=f2, ls2=ls2, f3=f3, ls3=ls3,
                    gap=gap, top0=top0, div=div, y3=y3, h3=h3, room=room, fits=fits)

    # 글자 크기의 기준. 높이만 보면 표지판이 세로로 길 때 글자가 폭을 넘쳐
    # 줄이 자꾸 늘어난다. 3:4 보다 좁으면 폭을 기준으로 잡는다.
    # (미리보기 900x1200 은 3:4 라서 예전과 똑같이 h 가 기준이 된다)
    base = min(h, int(w * 4 / 3))
    pl_ = plan(base)
    for _ in range(14):                 # 다 안 들어가면 통째로 줄여 다시 짠다
        if pl_["fits"] or base <= h * .3:
            break
        base = int(base * .92)
        pl_ = plan(base)

    top = pl_["top0"]
    if pl_["ls1"]:
        top = block(pl_["ls1"], pl_["f1"], top, ic) + pl_["gap"]
    if pl_["ls2"]:
        block(pl_["ls2"], pl_["f2"], top, ac)
    d.line([int(w * .22), pl_["div"], int(w * .78), pl_["div"]],
           fill=(218, 212, 200), width=max(2, h // 260))
    if pl_["ls3"]:
        block(pl_["ls3"], pl_["f3"],
              pl_["y3"] + max(0, (pl_["room"] - pl_["h3"]) // 2), ic)
    return im


def _warp(art, quad, size):
    w, h = art.size
    A, B = [], []
    for (x, y), (u, v) in zip(quad, [(0, 0), (w, 0), (w, h), (0, h)]):
        A += [[u, v, 1, 0, 0, 0, -x * u, -x * v], [0, 0, 0, u, v, 1, -y * u, -y * v]]
        B += [x, y]
    co = np.linalg.solve(np.array(A, float), np.array(B, float))
    inv = np.linalg.inv(np.append(co, 1).reshape(3, 3))
    inv /= inv[2, 2]
    return art.transform(size, Image.PERSPECTIVE, inv.ravel()[:8], Image.BICUBIC)


def poster_image(lines, out_path, w=900, h=1200, **design):
    """포스터를 정면 그림으로 만든다. 미리보기와 영상 합성에 같은 그림을 쓴다."""
    art = _poster_art(w, h, lines, **design)
    art.save(str(out_path))
    return Path(out_path)


def _sign_mask(im):
    """표지판으로 칠해둔 마젠타 픽셀. 없으면 None."""
    a = np.asarray(im.convert("RGB")).astype(int)
    r, g, b = a[..., 0], a[..., 1], a[..., 2]
    m = (r > 110) & (b > 110) & (r - g > 45) & (b - g > 30)
    return m if m.mean() > 0.02 else None


def bake_poster(still, lines, out_path, custom=None, **design):
    """장면 그림의 표지판 자리에 포스터를 심는다.

    영상으로 만들기 전에 한 장에만 하면 되므로, 매 프레임 위치를 찾을 필요가 없다.
    심어두면 생성 모델이 포스터를 장면의 일부로 다루어 캐릭터가 그 앞에 설 수 있다.
    """
    base = Image.open(str(still)).convert("RGB")
    quad = _find_quad(base)
    wq = int(max(np.linalg.norm(quad[1] - quad[0]), np.linalg.norm(quad[2] - quad[3])))
    hq = int(max(np.linalg.norm(quad[3] - quad[0]), np.linalg.norm(quad[2] - quad[1])))
    if custom:
        art = Image.open(str(custom)).convert("RGB").resize((wq * 3, hq * 3), Image.LANCZOS)
    else:
        art = _poster_art(wq * 3, hq * 3, lines, **design)
    layer = _warp(art, quad, base.size)

    # 붙이는 틀은 마젠타 픽셀 그대로 쓴다. 사각형으로 덮으면 모서리에 분홍이 남고,
    # 앞을 가린 손까지 덮어버린다. 마젠타만 덮으면 둘 다 해결된다.
    sm = _sign_mask(base)
    if sm is not None:
        from scipy import ndimage
        sm = ndimage.binary_dilation(sm, iterations=3)
        mk = Image.fromarray((sm * 255).astype(np.uint8), "L")
    else:
        mk = Image.new("L", base.size, 0)
        ImageDraw.Draw(mk).polygon([tuple(p) for p in quad], fill=255)
    mk = mk.filter(ImageFilter.GaussianBlur(1.0))
    base.paste(layer, (0, 0), mk)
    base.save(str(out_path))
    return Path(out_path)


def poster_text(src_video, lines, out_path, progress=None, custom=None, **design):
    """5) 빈 포스터에 글자를 원근 변환으로 얹는다. 무료이고 몇 번이든 다시 할 수 있다."""
    d = tempfile.mkdtemp()
    try:
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(src_video),
                        os.path.join(d, "f%04d.png")], check=True)
        fs = sorted(os.listdir(d))
        first = Image.open(os.path.join(d, fs[0]))
        quad = _find_quad(first)
        wq = int(max(np.linalg.norm(quad[1] - quad[0]), np.linalg.norm(quad[2] - quad[3])))
        hq = int(max(np.linalg.norm(quad[3] - quad[0]), np.linalg.norm(quad[2] - quad[1])))
        if custom:
            art = Image.open(str(custom)).convert("RGB").resize((wq * 3, hq * 3), Image.LANCZOS)
        else:
            art = _poster_art(wq * 3, hq * 3, lines, **design)
        layer = _warp(art, quad, first.size)
        mk = Image.new("L", first.size, 0)
        ImageDraw.Draw(mk).polygon([tuple(p) for p in quad], fill=255)
        mk = mk.filter(ImageFilter.GaussianBlur(1.2))
        for i, f in enumerate(fs):
            p = os.path.join(d, f)
            base = Image.open(p).convert("RGB")
            # 원래 종이의 음영을 곱해서 얹어야 조명이 맞는다
            shade = np.asarray(base).astype(float) / 255
            base.paste(Image.fromarray(np.clip(np.asarray(layer).astype(float)
                                               * (.55 + .45 * shade), 0, 255).astype(np.uint8)),
                       (0, 0), mk)
            base.save(p)
            if progress and i % 12 == 0:
                progress(i, len(fs))
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-framerate", "24",
                        "-i", os.path.join(d, "f%04d.png"), "-c:v", "libx264",
                        "-pix_fmt", "yuv420p", "-crf", "18", str(out_path)], check=True)
        return out_path
    finally:
        shutil.rmtree(d, ignore_errors=True)


def _dur(p):
    return float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                                 "-of", "csv=p=0", str(p)],
                                capture_output=True, text=True).stdout.strip())


def finish(cuts, ending, music, out_path, w=720, h=1280, fps=30):
    """컷들과 엔딩을 붙이고 음악을 깐다.

    concat 디먹서는 프레임레이트가 다르면 뒤 입력을 통째로 버린다
    (엔딩 4초가 사라졌던 원인). 필터로 맞춰서 붙인다.
    """
    clips = [str(p) for p in cuts] + ([str(ending)] if ending else [])
    cmd = ["ffmpeg", "-v", "error", "-y"]
    for p in clips:
        cmd += ["-i", p]
    n = len(clips)
    fc = ";".join("[%d:v]scale=%d:%d:flags=lanczos,fps=%d,setsar=1[v%d]" % (i, w, h, fps, i)
                  for i in range(n))
    fc += ";" + "".join("[v%d]" % i for i in range(n)) + "concat=n=%d:v=1:a=0[v]" % n
    if music:
        # 음악은 춤 구간까지만 깔고 엔딩 포스터에서는 끈다.
        # 페이드아웃 없이 그냥 끝난다 (포스터를 조용히 보여주는 편이 낫다).
        dance = sum(_dur(p) for p in cuts)
        total = dance + (_dur(ending) if ending else 0.0)
        cmd += ["-stream_loop", "-1", "-i", str(music)]     # 음악이 짧으면 이어 붙인다
        fc += (";[%d:a]atrim=0:%.2f,asetpts=N/SR/TB,afade=t=in:st=0:d=0.3,"
               "loudnorm=I=-16:TP=-1.5:LRA=11,apad,atrim=0:%.2f[a]"
               % (n, dance, total))
        cmd += ["-filter_complex", fc, "-map", "[v]", "-map", "[a]",
                "-c:a", "aac", "-b:a", "192k", "-shortest"]
    else:
        cmd += ["-filter_complex", fc, "-map", "[v]"]
    cmd += ["-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "18", str(out_path)]
    subprocess.run(cmd, check=True)
    return out_path


def extract_audio(video, out_path):
    """레퍼런스 영상의 소리를 뽑는다. 그 영상에서 동작을 따왔으므로 박자가 정확히 맞는다."""
    r = subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(video), "-vn",
                        "-ac", "2", "-ar", "44100", "-b:a", "192k", str(out_path)])
    return Path(out_path) if r.returncode == 0 and Path(out_path).exists() else None


def has_audio(video):
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a",
                          "-show_entries", "stream=index", "-of", "csv=p=0", str(video)],
                         capture_output=True, text=True).stdout.strip()
    return bool(out)


# ---- 카메라 무빙 -------------------------------------------------------------
# Kling 프롬프트로 카메라를 지시하면 결과가 들쭉날쭉하다. 생성된 영상에 나중에
# 입히면 강도를 몇 번이든 바꿔볼 수 있고 재생성이 필요 없다.

# 움직임은 zoompan 표현식으로 만든다. on=현재 프레임, N=전체 프레임.
#   z  = 확대 배율,  x/y = 잘라낼 위치
# 자막을 얹기 전에 걸어야 자막이 같이 흔들리지 않는다.
CAMERA = {
    "none": dict(label="없음", zoom=0.0, pan=0, kind="still"),
    "push": dict(label="천천히 밀기", zoom=0.14, pan=0, kind="push"),
    "sway": dict(label="밀며 좌우로 흐르기", zoom=0.13, pan=58, kind="sway"),
    "sway_strong": dict(label="밀며 크게 흐르기", zoom=0.24, pan=112, kind="sway"),
    "orbit": dict(label="빙 돌듯 움직이기", zoom=0.18, pan=90, kind="orbit"),
    "punch": dict(label="구간마다 확 당기기", zoom=0.26, pan=44, kind="punch"),
    "handheld": dict(label="손으로 든 느낌", zoom=0.16, pan=52, kind="handheld"),
}
KLING_CAMERA_PROMPT = {
    "none": "",
    "push": "The camera slowly pushes in toward the character.",
    "sway": "The camera slowly pushes in while drifting from side to side.",
    "sway_strong": "The camera sweeps from side to side while pushing in, wide dynamic movement.",
    "orbit": "The camera arcs around the character in a slow orbit while it dances.",
    "punch": "The camera pushes in sharply on the beat, then pulls back and pushes in again.",
    "handheld": "Handheld camera with lively natural shake, following the character closely.",
}


def _cam_expr(kind, zoom, pan, n):
    """kind 별 z / x / y 표현식. n 은 전체 프레임 수."""
    cx = "iw/2-(iw/zoom/2)"
    cy = "ih/2-(ih/zoom/2)"
    if kind == "push":
        return "1+%.3f*(on/%d)" % (zoom, n), cx, cy
    if kind == "sway":
        return ("1+%.3f*(on/%d)" % (zoom, n),
                "%s+%d*sin(2*PI*on/%d)" % (cx, pan, n),
                "%s+%d*sin(4*PI*on/%d)" % (cy, pan // 3, n))
    if kind == "orbit":
        # 좌우와 상하를 90도 어긋내면 원을 그리듯 돈다
        return ("1+%.3f*(0.5-0.5*cos(2*PI*on/%d))" % (zoom, n),
                "%s+%d*sin(4*PI*on/%d)" % (cx, pan, n),
                "%s+%d*cos(4*PI*on/%d)" % (cy, pan // 2, n))
    if kind == "punch":
        # 톱니파: 일정 구간마다 당겼다가 원위치
        seg = max(24, n // 6)
        return ("1+%.3f*mod(on,%d)/%d" % (zoom, seg, seg),
                "%s+%d*sin(2*PI*on/%d)" % (cx, pan, n),
                cy)
    if kind == "handheld":
        # 주기가 다른 흔들림을 겹쳐 사람 손처럼 불규칙하게 만든다
        return ("1+%.3f*(on/%d)" % (zoom, n),
                "%s+%d*sin(2*PI*on/%d)+%d*sin(2*PI*on/%d)"
                % (cx, pan, n, max(4, pan // 3), max(12, n // 9)),
                "%s+%d*sin(2*PI*on/%d)+%d*cos(2*PI*on/%d)"
                % (cy, pan // 2, max(18, n // 7), max(3, pan // 4), max(9, n // 13)))
    return "1", cx, cy


def camera_move(src, out_path, level="sway", w=720, h=1280, fps=30):
    """생성된 영상에 카메라 움직임을 입힌다.

    Kling 프롬프트로 지시하면 결과가 들쭉날쭉하지만, 이건 확실하고
    강도를 몇 번이든 바꿔볼 수 있으며 재생성이 필요 없다.
    """
    c = CAMERA.get(level) or CAMERA["sway"]
    if c["kind"] == "still":
        shutil.copy(str(src), str(out_path))
        return Path(out_path)
    n = max(2, int(round(_dur(src) * fps)))
    z, x, y = _cam_expr(c["kind"], c["zoom"], c["pan"], n)
    vf = ("scale=%d:%d:flags=lanczos,"
          "zoompan=z='%s':x='%s':y='%s':d=1:s=%dx%d:fps=%d"
          % (w * 2, h * 2, z, x, y, w, h, fps))
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(src), "-vf", vf,
                    "-c:a", "copy", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                    "-crf", "18", str(out_path)], check=True)
    return Path(out_path)


# ---- 가사 자막 ---------------------------------------------------------------
# 노래는 말이 아니라서 음성인식이 자주 틀린다. 그래서 자동 생성은 초안일 뿐이고
# 사람이 고쳐야 한다. 시간은 대체로 맞으므로 글자만 손보면 된다.

LINE_END = chr(10)
STT = "fal-ai/elevenlabs/speech-to-text"
STT_PRICE = 0.02

# 자막 한 줄을 끊는 기준. 노래는 쉬는 구간이 뚜렷해서 간격으로 잘 갈린다.
LINE_GAP = 0.7        # 이만큼 쉬면 줄을 바꾼다
LINE_CHARS = 16       # 이보다 길어지면 줄을 바꾼다
LINE_SECS = 4.0


def transcribe(audio_path, language="ko"):
    """가사 초안. [{start, end, text}] 를 돌려준다.

    fal 의 whisper 는 이 음원에서 빈 결과를 냈고 wizper 는 많이 틀렸다.
    ElevenLabs 가 가장 정확했고 단어별 시각까지 준다. 그래도 노래는 말이 아니라서
    자주 틀린다 — 그래서 사람이 고치는 단계가 반드시 필요하다.
    """
    fal = _fal()
    tmp = tempfile.mkdtemp()
    try:
        args = {"audio_url": fal.upload_file(_ascii_copy(audio_path, tmp, "audio.mp3"))}
        if language:
            args["language_code"] = language
        r = retry(lambda: fal.subscribe(STT, arguments=args, with_logs=False))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return group_words([w for w in (r.get("words") or []) if w.get("type") == "word"],
                       (r.get("text") or "").strip())


def group_words(words, fallback=""):
    """단어들을 자막 줄로 묶는다. 쉬는 구간·길이·시간으로 끊는다."""
    if not words:
        return [{"start": 0.0, "end": 4.0, "text": fallback}] if fallback else []
    lines, cur = [], []

    def flush():
        if cur:
            lines.append({"start": round(float(cur[0]["start"]), 2),
                          "end": round(float(cur[-1]["end"]), 2),
                          "text": " ".join(w["text"].strip() for w in cur)})
            cur.clear()

    for w in words:
        if cur:
            gap = float(w["start"]) - float(cur[-1]["end"])
            chars = sum(len(x["text"]) for x in cur) + len(w["text"])
            span = float(w["end"]) - float(cur[0]["start"])
            if gap > LINE_GAP or chars > LINE_CHARS or span > LINE_SECS:
                flush()
        cur.append(w)
    flush()
    # 너무 짧게 스치는 줄은 최소 1초는 보이게 한다
    for i, l in enumerate(lines):
        if l["end"] - l["start"] < 1.0:
            nxt = lines[i + 1]["start"] if i + 1 < len(lines) else l["start"] + 1.2
            l["end"] = round(min(l["start"] + 1.2, max(nxt - 0.05, l["end"])), 2)
    return lines


def _ass_time(t):
    t = max(0.0, float(t))
    h, m = int(t // 3600), int(t % 3600 // 60)
    return "%d:%02d:%05.2f" % (h, m, t % 60)


SUB_STYLES = {
    # 그림자만으로는 밝은 배경에서 묻힌다. 아주 얇은 외곽선을 같이 줘서
    # 가는 글씨 느낌은 유지하면서 읽히게 한다.
    "soft": dict(label="얇고 넓게 (하늘 위 자막)", bold=0, spacing=0.09,
                 outline=0.045, shadow=2.0, size=0.032, font="Noto Sans KR Light"),
    # 글꼴 이름은 서버에도 있는 것으로 둔다. 윈도우 전용 이름(맑은 고딕)을 쓰면
    # 리눅스 서버에서 글꼴을 못 찾아 자막이 통째로 안 보인다 (겪었다).
    "bold": dict(label="굵고 또렷하게", bold=-1, spacing=0.0,
                 outline=0.14, shadow=1.0, size=0.042, font="Noto Sans KR"),
    "serif": dict(label="명조체", bold=0, spacing=0.05,
                  outline=0.0, shadow=2.0, size=0.033, font="Noto Serif KR Light"),
}
SUB_FONTS_FALLBACK = ["맑은 고딕", "Noto Sans KR Light", "Noto Sans KR",
                      "Noto Sans KR Thin", "Noto Serif KR Light", "굴림"]


# 설치하지 않고 쓰는 글꼴 폴더. 여기에 ttf/otf 를 넣기만 하면 목록에 잡힌다.
BUNDLED_FONTS = Path(__file__).parent / "fonts"
FONT_DIRS = [str(BUNDLED_FONTS),
             r"C:\Windows\Fonts",
             os.path.join(os.environ.get("LOCALAPPDATA", ""), "Microsoft", "Windows", "Fonts"),
             "/usr/share/fonts",                       # 리눅스 서버
             os.path.expanduser("~/.fonts")]
_KO_PROBE = (0xAC00, 0xB098, 0xD55C, 0xAE00)      # 가 나 한 글
_FONT_CACHE = []


FONT_CACHE_FILE = Path(__file__).parent / "fonts_cache.json"
# 이름 -> {"path": 글꼴파일, "var": 가변글꼴 안의 굵기 이름}
#
# 목록을 만드는 쪽(system_fonts)과 파일을 찾는 쪽이 **서로 다른 규칙**을 쓰고
# 있었다. 목록은 가변 글꼴 파일 안의 굵기까지 꺼내 쓰는데("Gothic A1 Black"),
# 파일 찾기는 이름표만 보니 그런 이름은 못 찾고 조용히 기본 글꼴로 떨어졌다.
# 그래서 글꼴을 바꿔도 포스터가 그대로였다. 훑을 때 지도도 같이 만든다.
FONT_MAP_FILE = Path(__file__).parent / "fonts_map.json"
_FONT_MAP = {}


def _is_ko(n):
    return any("가" <= ch <= "힣" for ch in n)


def _pick_name(n1, n16):
    """한 파일이 이름을 여러 개 갖는다.

    nameID 1 은 굵기까지 구분되는 이름("Noto Sans KR Light"),
    nameID 16 은 묶음 이름("Noto Sans KR")이다. 16 을 쓰면 Light·Thin 이
    전부 한 덩어리로 뭉쳐 버리므로 1 을 우선한다.
    한글 이름과 영문 이름이 같이 있으면(HY궁서 / HYGungSo-Bold) 한글을 쓴다.
    """
    for pool in (n1, n16):
        if not pool:
            continue
        ko = [x for x in pool if _is_ko(x)]
        return sorted(ko or pool, key=len)[0]
    return None


def system_fonts(rescan=False):
    """한글이 실제로 그려지는 글꼴만 고른다.

    이름으로 거르면(맑은/나눔/Noto…) 안상수·휴먼·한양 같은 서체를 통째로 놓친다.
    글꼴 파일을 열어 한글 글리프가 들어 있는지 직접 확인한다.
    훑는 데 10초쯤 걸려서 결과를 파일에 저장해둔다.
    """
    if rescan:
        _FONT_CACHE.clear()
        _FONT_MAP.clear()
    elif _FONT_CACHE:
        return list(_FONT_CACHE)
    if not rescan and FONT_CACHE_FILE.exists():
        try:
            import json
            cached = json.loads(FONT_CACHE_FILE.read_text(encoding="utf-8"))
            if cached:
                _FONT_CACHE.extend(cached)
                return list(_FONT_CACHE)
        except Exception:
            pass
    try:
        from fontTools.ttLib import TTFont, TTCollection
    except ImportError:
        return list(SUB_FONTS_FALLBACK)

    def families(f):
        n1, n16 = set(), set()
        try:
            for r in f["name"].names:
                if r.nameID not in (1, 16):
                    continue
                try:
                    v = r.toUnicode().strip()
                except Exception:
                    continue
                if v:
                    (n1 if r.nameID == 1 else n16).add(v)
        except Exception:
            pass
        return n1, n16

    def instances(f, base):
        """가변 글꼴은 파일 하나에 굵기가 다 들어 있다(Noto Sans KR Light/Thin/…).
        내부 목록을 읽어야 굵기별로 고를 수 있다."""
        out = []
        try:
            if "fvar" not in f:
                return out
            names = {r.nameID: r for r in f["name"].names}
            for inst in f["fvar"].instances:
                r = names.get(inst.subfamilyNameID)
                if not r:
                    continue
                sub = r.toUnicode().strip()
                if not sub or sub.lower() in ("regular", "normal"):
                    continue
                # 굵기 이름도 같이 돌려준다. PIL 로 그릴 때 이게 있어야
                # 같은 파일에서 Light 와 Black 을 구분해 그릴 수 있다.
                out.append(("%s %s" % (base, sub), sub))
        except Exception:
            pass
        return out

    def korean(f):
        try:
            cs = set()
            for t in f["cmap"].tables:
                if t.isUnicode():
                    cs |= set(t.cmap.keys())
            return all(c in cs for c in _KO_PROBE)
        except Exception:
            return False

    import glob
    picked = set()
    for d in FONT_DIRS:
        if not d or not os.path.isdir(d):
            continue
        for ext in ("*.ttf", "*.otf", "*.ttc"):
            for path in glob.glob(os.path.join(d, ext)):
                try:
                    fonts = (TTCollection(path).fonts if path.lower().endswith(".ttc")
                             else [TTFont(path, lazy=True, fontNumber=0)])
                    # 묶음 파일(.ttc)은 한 파일에 글꼴이 여러 개 들어 있다.
                    # 몇 번째인지 적어두지 않으면 늘 첫 번째만 열려서
                    # 굴림·굴림체·돋움이 전부 같은 모양으로 그려진다.
                    for idx, f in enumerate(fonts):
                        if korean(f):
                            n = _pick_name(*families(f))
                            if n:
                                picked.add(n)
                                _FONT_MAP.setdefault(n, {"path": path, "var": None,
                                                         "idx": idx})
                                for nm, sub in instances(f, n):
                                    picked.add(nm)
                                    _FONT_MAP.setdefault(nm, {"path": path, "var": sub,
                                                              "idx": idx})
                except Exception:
                    pass
    names = sorted(picked)
    head = [n for n in ("맑은 고딕", "Noto Sans KR Light", "Noto Sans KR",
                        "나눔고딕", "함초롬돋움", "굴림", "바탕") if n in names]
    _FONT_CACHE.extend(head + [n for n in names if n not in head])
    try:
        import json
        FONT_CACHE_FILE.write_text(json.dumps(_FONT_CACHE, ensure_ascii=False),
                                   encoding="utf-8")
        FONT_MAP_FILE.write_text(json.dumps(_FONT_MAP, ensure_ascii=False),
                                 encoding="utf-8")
    except Exception:
        pass        # 람다는 읽기 전용이라 못 적는다. 메모리에 들고 쓰면 된다.
    return list(_FONT_CACHE) or list(SUB_FONTS_FALLBACK)


def _ass_color(hex_color, alpha="00"):
    """#RRGGBB -> ASS 의 &HAABBGGRR (ASS 는 색 순서가 뒤집혀 있다)."""
    c = (hex_color or "#FFFFFF").lstrip("#")
    if len(c) != 6:
        c = "FFFFFF"
    try:
        int(c, 16)
    except ValueError:
        c = "FFFFFF"
    return "&H%s%s%s%s" % (alpha, c[4:6].upper(), c[2:4].upper(), c[0:2].upper())


def write_ass(subs, path, w=720, h=1280, style="soft", font=None, size=None, pos="top",
              color=None, outline_color=None, outline=None, bold=None):
    """자막 파일.

    루터스 영상처럼 화면 위쪽에 얇은 글씨를 넓은 자간으로 놓는 것이 기본이다.
    외곽선 대신 옅은 그림자를 쓰면 하늘 같은 밝은 배경에서도 부드럽게 읽힌다.
    """
    st = dict(SUB_STYLES.get(style) or SUB_STYLES["soft"])
    fam = font or st["font"]
    px = max(18, int(h * (size if size else st["size"])))
    align = 8 if pos == "top" else 2          # 8 = 위 가운데, 2 = 아래 가운데
    margin = int(h * (0.13 if pos == "top" else 0.11))
    ow = st["outline"] if outline is None else float(outline)
    bd = st["bold"] if bold is None else (-1 if bold else 0)
    fg = _ass_color(color or "#FFFFFF")
    oc = _ass_color(outline_color or "#181818")
    head = [
        "[Script Info]",
        "ScriptType: v4.00+",
        "PlayResX: %d" % w,
        "PlayResY: %d" % h,
        "WrapStyle: 2",
        "ScaledBorderAndShadow: yes",
        "",
        "[V4+ Styles]",
        ("Format: Name, Fontname, Fontsize, PrimaryColour, OutlineColour, BackColour, "
         "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, "
         "Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding"),
        ("Style: main,%s,%d,%s,%s,&H50000000,"
         "%d,0,0,0,100,100,%.1f,0,1,%.1f,%.1f,%d,40,40,%d,1"
         % (fam, px, fg, oc, bd, px * st["spacing"],
            px * ow, st["shadow"], align, margin)),
        "",
        "[Events]",
        ("Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, "
         "Effect, Text"),
    ]
    for s in subs:
        txt = str(s.get("text", "")).replace("{", "(").replace("}", ")").strip()
        txt = " ".join(txt.split())
        if not txt:
            continue
        head.append("Dialogue: 0,%s,%s,main,,0,0,0,,%s"
                    % (_ass_time(s["start"]), _ass_time(s["end"]), txt))
    Path(path).write_text(LINE_END.join(head) + LINE_END, encoding="utf-8")
    return path


def burn_subs(video, subs, out_path, w=720, h=1280, **style):
    """자막을 영상에 굽는다. 무료이고, 글자를 고쳐 몇 번이든 다시 구울 수 있다.

    ffmpeg 의 subtitles 필터는 윈도우 경로의 콜론을 싫어한다.
    임시 폴더로 옮기고 그 폴더에서 실행해 경로 문제를 피한다.
    """
    d = tempfile.mkdtemp()
    app_dir = Path(__file__).parent
    try:
        shutil.copy(str(video), os.path.join(d, "in.mp4"))
        # ffmpeg 필터 인자에 윈도우 경로(C:)를 그대로 넣으면 콜론 때문에 깨진다.
        # 그래서 윈도우에서는 app 폴더에서 실행하고 상대경로만 쓴다.
        # 리눅스(람다)에서는 app 폴더가 읽기 전용이라 거기에 파일을 못 만든다.
        # 그쪽에서는 임시 폴더에 만들고 글꼴 폴더만 전체 경로로 가리킨다.
        if os.name == "nt":
            ass = app_dir / ("_sub_%s.ass" % os.path.basename(d)[-8:])
            vf = "subtitles=%s:fontsdir=fonts" % ass.name
            run_in = str(app_dir)
        else:
            ass = Path(d) / "sub.ass"
            vf = "subtitles=sub.ass:fontsdir=%s" % (app_dir / "fonts")
            run_in = d
        write_ass(subs, ass, w, h, **style)
        try:
            subprocess.run(["ffmpeg", "-v", "error", "-y",
                            "-i", os.path.join(d, "in.mp4"), "-vf", vf, "-c:a", "copy",
                            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "18",
                            os.path.join(d, "out.mp4")],
                           cwd=run_in, check=True)
        finally:
            ass.unlink(missing_ok=True)
        shutil.copy(os.path.join(d, "out.mp4"), str(out_path))
        return Path(out_path)
    finally:
        shutil.rmtree(d, ignore_errors=True)


_FONT_FILE_CACHE = {}


FONT_SHEET_W = 440          # 견본 한 줄의 너비
FONT_SHEET_ROW = 46         # 견본 한 줄의 높이


def font_sheet(names, out_path, sample="다람쥐 헌 쳇바퀴에 타고파"):
    """글꼴 목록을 **실제로 그려서** 한 장에 쌓는다.

    화면은 글꼴 이름만 알 뿐 그 글꼴 파일은 없다. CSS 로 미리보기를 하면
    브라우저에 없는 글꼴이라 전부 기본 글꼴로 떨어져, 72개가 똑같아 보였다.
    서버가 그려서 보내면 고르기 전에 진짜 모양을 볼 수 있다.

    목록(system_fonts)과 **같은 순서**로 쌓는다. 화면은 몇 번째 줄인지로 찾는다.
    """
    W, R = FONT_SHEET_W, FONT_SHEET_ROW
    im = Image.new("RGB", (W, R * max(1, len(names))), (255, 255, 255))
    d = ImageDraw.Draw(im)
    for i, nm in enumerate(names):
        size = 26
        while True:
            f = font_of(nm, size, bold=False)
            bb = d.textbbox((0, 0), sample, font=f)
            if bb[2] - bb[0] <= W - 20 or size <= 11:
                break
            size -= 1
        y = i * R + (R - (bb[3] - bb[1])) // 2 - bb[1]
        d.text((10, y), sample, font=f, fill=(38, 34, 28))
    im.save(str(out_path))
    return out_path


def _font_map():
    """이름 -> 글꼴 파일 지도. 없으면 한 번 훑어서 만든다."""
    if _FONT_MAP:
        return _FONT_MAP
    try:
        import json
        if FONT_MAP_FILE.exists():
            got = json.loads(FONT_MAP_FILE.read_text(encoding="utf-8"))
            # 지도에는 **그 컴퓨터의 파일 경로**가 들어 있다. 다른 데서 만든
            # 것을 물려받으면 경로가 하나도 안 맞아 전부 기본 글꼴로 떨어진다.
            # 몇 개만 찍어 보고 없으면 통째로 버린다.
            if got and any(os.path.exists(e.get("path", ""))
                           for e in list(got.values())[:5]):
                _FONT_MAP.update(got)
    except Exception:
        pass
    if not _FONT_MAP:
        system_fonts(rescan=True)      # 목록과 지도를 같이 만든다
    return _FONT_MAP


def font_of(name, size, bold=True):
    """글꼴 이름으로 PIL 글꼴을 연다.

    가변 글꼴은 파일 하나에 굵기가 다 들어 있어서, 파일만 열면 늘 기본 굵기로
    그려진다. 지도에 적어둔 굵기 이름을 입혀야 Light 와 Black 이 달라진다.
    """
    fallback = FONT_B if bold else FONT_R
    e = _font_map().get(name) if name else None
    if not e:
        return ImageFont.truetype(fallback, size)
    try:
        f = ImageFont.truetype(e["path"], size, index=e.get("idx", 0))
    except Exception as err:
        # 조용히 기본 글꼴로 떨어지면 "글꼴을 바꿔도 안 바뀐다" 로만 보인다.
        print("글꼴 열기 실패: %s (%s) %s" % (name, e.get("path"), err))
        return ImageFont.truetype(fallback, size)
    if e.get("var"):
        try:
            f.set_variation_by_name(e["var"])
        except Exception as err:
            print("굵기 적용 실패: %s / %s - %s" % (name, e.get("var"), err))
    return f


def font_file(family):
    """글꼴 이름으로 파일 경로를 찾는다 (옛 이름). 지도에서 꺼내 쓴다."""
    e = _font_map().get(family) if family else None
    if e:
        return e["path"]
    if not family:
        return None
    if family in _FONT_FILE_CACHE:
        return _FONT_FILE_CACHE[family]
    try:
        from fontTools.ttLib import TTFont, TTCollection
    except ImportError:
        return None
    import glob
    hit = None
    for d in FONT_DIRS:
        if not d or not os.path.isdir(d):
            continue
        for ext in ("*.ttf", "*.otf", "*.ttc"):
            for path in glob.glob(os.path.join(d, ext)):
                try:
                    fonts = (TTCollection(path).fonts if path.lower().endswith(".ttc")
                             else [TTFont(path, lazy=True, fontNumber=0)])
                    for f in fonts:
                        names = set()
                        for r in f["name"].names:
                            if r.nameID in (1, 4, 16):
                                try:
                                    names.add(r.toUnicode().strip())
                                except Exception:
                                    pass
                        if family in names:
                            hit = path
                            break
                except Exception:
                    pass
                if hit:
                    break
            if hit:
                break
        if hit:
            break
    _FONT_FILE_CACHE[family] = hit
    return hit


_BUNDLE_MAP = {}


def bundled_font_map():
    """번들 글꼴의 (이름 -> 파일명). 브라우저 미리보기용 @font-face 를 만들 때 쓴다."""
    if _BUNDLE_MAP:
        return dict(_BUNDLE_MAP)
    try:
        from fontTools.ttLib import TTFont
    except ImportError:
        return {}
    import glob
    for path in sorted(glob.glob(os.path.join(str(BUNDLED_FONTS), "*.ttf"))
                       + glob.glob(os.path.join(str(BUNDLED_FONTS), "*.otf"))):
        try:
            f = TTFont(path, lazy=True, fontNumber=0)
            n1, n16 = set(), set()
            for r in f["name"].names:
                if r.nameID in (1, 16):
                    try:
                        v = r.toUnicode().strip()
                    except Exception:
                        continue
                    if v:
                        (n1 if r.nameID == 1 else n16).add(v)
            nm = _pick_name(n1, n16)
            if nm and nm not in _BUNDLE_MAP:
                _BUNDLE_MAP[nm] = os.path.basename(path)
        except Exception:
            pass
    return dict(_BUNDLE_MAP)
