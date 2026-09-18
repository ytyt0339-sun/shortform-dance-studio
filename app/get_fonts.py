# -*- coding: utf-8 -*-
"""자유 라이선스(OFL) 한글 글꼴을 app/fonts 에 받는다.

시스템에 설치하지 않는다. ffmpeg 에는 fontsdir 로 알려주고,
브라우저 미리보기에는 @font-face 로 서빙한다.
"""
import re, sys, io, time
from pathlib import Path
import httpx
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', line_buffering=True)

OUT = Path("app/fonts"); OUT.mkdir(parents=True, exist_ok=True)
UA = {"User-Agent": "Mozilla/5.0"}          # 이 UA 라야 woff2 대신 ttf 를 준다

FAMILIES = [
    ("Do Hyeon", None), ("Jua", None), ("Black Han Sans", None),
    ("Gowun Dodum", None), ("Gowun Batang", "wght@400;700"),
    ("Nanum Gothic", "wght@400;700;800"), ("Nanum Myeongjo", "wght@400;700;800"),
    ("Nanum Pen Script", None), ("Nanum Brush Script", None),
    ("Gaegu", "wght@300;400;700"), ("Hi Melody", None), ("Poor Story", None),
    ("Dokdo", None), ("East Sea Dokdo", None), ("Cute Font", None),
    ("Single Day", None), ("Stylish", None), ("Yeon Sung", None),
    ("Gugi", None), ("Kirang Haerang", None), ("Dongle", "wght@300;400;700"),
    ("Song Myung", None), ("Sunflower", "wght@300;500;700"),
    ("Gamja Flower", None), ("Gothic A1", "wght@300;400;700;900"),
    ("IBM Plex Sans KR", "wght@300;400;700"), ("Hahmlet", "wght@400;700"),
    ("Nanum Gothic Coding", None), ("Diphylleia", None), ("Moirai One", None),
]

ok = fail = skip = 0
with httpx.Client(headers=UA, timeout=40, follow_redirects=True) as c:
    for fam, axis in FAMILIES:
        q = fam.replace(" ", "+") + (":" + axis if axis else "")
        try:
            css = c.get(f"https://fonts.googleapis.com/css2?family={q}&display=swap").text
        except Exception as e:
            print("  %-22s CSS 실패 %s" % (fam, str(e)[:40])); fail += 1; continue
        urls = sorted(set(re.findall(r"url\((https://[^)]+\.ttf)\)", css)))
        if not urls:
            print("  %-22s ttf 없음" % fam); fail += 1; continue
        for i, u in enumerate(urls):
            name = "%s%s.ttf" % (fam.replace(" ", ""), "" if len(urls) == 1 else "-%d" % (i + 1))
            p = OUT / name
            if p.exists() and p.stat().st_size > 10000:
                skip += 1; continue
            try:
                p.write_bytes(c.get(u).content)
                ok += 1
            except Exception as e:
                print("  %-22s 받기 실패 %s" % (fam, str(e)[:40])); fail += 1
        print("  %-22s %d개" % (fam, len(urls)))
        time.sleep(0.15)

print("\n받음 %d · 건너뜀 %d · 실패 %d" % (ok, skip, fail))
print("총 %d개 파일 · %.1fMB" % (len(list(OUT.glob('*.ttf'))),
                                sum(f.stat().st_size for f in OUT.glob('*.ttf')) / 1e6))
