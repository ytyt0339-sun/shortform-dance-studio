# -*- coding: utf-8 -*-
"""엔딩 포스터에 글자를 얹는다.

포스터는 빈 상태로 생성해두고 글자는 여기서 넣는다.
  - 한글을 이미지 모델에 안 맡기므로 절대 안 깨진다
  - 날짜/행사를 바꿔도 재생성이 필요 없다 (이 스크립트만 다시 돌리면 됨)
카메라가 고정된 영상이라 꼭짓점을 한 번만 찾아 전 프레임에 같은 변환을 쓴다.

사용:  python app/poster_text.py "한양대 대동제" "5월 20일 (화)" "대운동장에서 만나요!"
"""
import os, sys, io, subprocess, tempfile, shutil
import numpy as np
from PIL import Image, ImageDraw, ImageFont
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', line_buffering=True)

SRC = "_demo_엔딩/ending_raw.mp4"
OUT = "_demo_엔딩/ending.mp4"
FONT_B = "C:/Windows/Fonts/malgunbd.ttf"
FONT_R = "C:/Windows/Fonts/malgun.ttf"
ACCENT = (232, 178, 46)      # 마스코트 노랑
INK    = (38, 34, 28)

L1 = sys.argv[1] if len(sys.argv) > 1 else "한양대 대동제"
L2 = sys.argv[2] if len(sys.argv) > 2 else "5월 20일 (화)"
L3 = sys.argv[3] if len(sys.argv) > 3 else "대운동장에서 만나요!"


def find_quad(im):
    """포스터의 네 꼭짓점. 밝고 채도 낮은 최대 영역을 찾아 극점을 취한다."""
    from scipy import ndimage
    a = np.asarray(im.convert("RGB")).astype(int)
    mx, mn = a.max(2), a.min(2)
    m = (mx > 195) & ((mx - mn) < 38)
    lab, n = ndimage.label(m)
    k = int(np.argmax(ndimage.sum(m, lab, range(1, n + 1)))) + 1
    ys, xs = np.where(lab == k)
    pts = np.stack([xs, ys], 1).astype(float)
    s, d = pts.sum(1), np.diff(pts, axis=1).ravel()
    # 좌상=합 최소, 우하=합 최대, 우상=차 최소, 좌하=차 최대
    return np.array([pts[s.argmin()], pts[d.argmin()], pts[s.argmax()], pts[d.argmax()]])


def poster_art(w, h):
    """포스터에 들어갈 그림. 정면 기준으로 그리고 나중에 원근을 입힌다."""
    im = Image.new("RGB", (w, h), (252, 250, 245))
    d = ImageDraw.Draw(im)
    d.rectangle([0, 0, w, int(h * .16)], fill=ACCENT)
    safe = w * .84                      # 포스터 폭을 넘으면 글자가 잘린다

    def fit(path, txt, size):
        """폭에 들어갈 때까지 글자 크기를 줄인다."""
        f = ImageFont.truetype(path, size)
        while size > 8:
            bb = d.textbbox((0, 0), txt, font=f)
            if bb[2] - bb[0] <= safe:
                return f
            size = int(size * .94)
            f = ImageFont.truetype(path, size)
        return f

    f1 = fit(FONT_B, L1, int(h * .095))
    f2 = fit(FONT_B, L2, int(h * .135))
    f3 = fit(FONT_R, L3, int(h * .065))

    def mid(txt, f, y, fill):
        bb = d.textbbox((0, 0), txt, font=f)
        d.text(((w - (bb[2] - bb[0])) / 2 - bb[0], y), txt, font=f, fill=fill)

    mid(L1, f1, int(h * .30), INK)
    mid(L2, f2, int(h * .44), ACCENT)
    d.line([int(w * .22), int(h * .63), int(w * .78), int(h * .63)], fill=(218, 212, 200), width=max(2, h // 260))
    mid(L3, f3, int(h * .69), INK)
    return im


def warp(art, quad, size):
    """정면 그림을 네 꼭짓점 위치로 원근 변환."""
    w, h = art.size
    src = [(0, 0), (w, 0), (w, h), (0, h)]
    A, B = [], []
    for (x, y), (u, v) in zip(quad, src):          # 목적->원본 매핑이 필요하다
        A += [[u, v, 1, 0, 0, 0, -x * u, -x * v], [0, 0, 0, u, v, 1, -y * u, -y * v]]
        B += [x, y]
    co = np.linalg.solve(np.array(A, float), np.array(B, float))
    inv = np.linalg.inv(np.append(co, 1).reshape(3, 3))
    inv /= inv[2, 2]
    return art.transform(size, Image.PERSPECTIVE, inv.ravel()[:8], Image.BICUBIC)


d = tempfile.mkdtemp()
try:
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", SRC, os.path.join(d, "f%04d.png")], check=True)
    fs = sorted(os.listdir(d))
    first = Image.open(os.path.join(d, fs[0]))
    quad = find_quad(first)
    print("포스터 꼭짓점:", [tuple(map(int, p)) for p in quad])

    wq = int(max(np.linalg.norm(quad[1] - quad[0]), np.linalg.norm(quad[2] - quad[3])))
    hq = int(max(np.linalg.norm(quad[3] - quad[0]), np.linalg.norm(quad[2] - quad[1])))
    art = poster_art(wq * 3, hq * 3)
    layer = warp(art, quad, first.size)

    # 포스터 영역만 남기는 마스크 (가장자리 살짝 부드럽게)
    mk = Image.new("L", first.size, 0)
    ImageDraw.Draw(mk).polygon([tuple(p) for p in quad], fill=255)
    from PIL import ImageFilter
    mk = mk.filter(ImageFilter.GaussianBlur(1.2))

    for f in fs:
        p = os.path.join(d, f)
        base = Image.open(p).convert("RGB")
        # 원래 종이의 음영을 곱해서 얹어야 조명이 맞는다
        shade = np.asarray(base).astype(float) / 255
        tinted = Image.fromarray(np.clip(np.asarray(layer).astype(float)
                                         * (0.55 + 0.45 * shade), 0, 255).astype(np.uint8))
        base.paste(tinted, (0, 0), mk)
        base.save(p)

    subprocess.run(["ffmpeg", "-v", "error", "-y", "-framerate", "24",
                    "-i", os.path.join(d, "f%04d.png"), "-i", SRC,
                    "-map", "0:v", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                    "-crf", "18", OUT], check=True)
    print("완성:", OUT)
finally:
    shutil.rmtree(d, ignore_errors=True)
