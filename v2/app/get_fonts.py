# -*- coding: utf-8 -*-
"""자유 라이선스(OFL) 한글 글꼴을 app/fonts 에 받는다.

시스템에 설치하지 않는다. ffmpeg 에는 fontsdir 로 알려주고,
브라우저 미리보기에는 @font-face 로 서빙한다.

목록은 구글 폰트 메타데이터에서 '한글(korean) 지원' 으로 걸러 가져온다.
손으로 적어두면 새 글꼴이 나와도 모르고, 굵기를 빠뜨리기 쉽다.
굵기까지 받는 이유: 이름이 굵기별로 갈려서(Noto Sans KR Light / Bold …)
사용자가 고를 수 있는 글꼴 수가 그만큼 늘어난다.
"""
import re, sys, io, time, json
from pathlib import Path
import httpx

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', line_buffering=True)

OUT = Path("app/fonts"); OUT.mkdir(parents=True, exist_ok=True)
UA = {"User-Agent": "Mozilla/5.0"}          # 이 UA 라야 woff2 대신 ttf 를 준다
META = "https://fonts.google.com/metadata/fonts"
MAX_WEIGHTS = 6        # 한 패밀리에서 받을 굵기 수 (Noto 는 9개라 다 받으면 무겁다)

# 메타데이터를 못 받을 때 쓰는 최소 목록 (예전에 쓰던 것)
FALLBACK = [("Do Hyeon", ["400"]), ("Jua", ["400"]), ("Black Han Sans", ["400"]),
            ("Gowun Dodum", ["400"]), ("Gowun Batang", ["400", "700"]),
            ("Nanum Gothic", ["400", "700", "800"]),
            ("Nanum Myeongjo", ["400", "700", "800"]),
            ("Nanum Pen Script", ["400"]), ("Nanum Brush Script", ["400"]),
            ("Gaegu", ["300", "400", "700"]), ("Hi Melody", ["400"]),
            ("Poor Story", ["400"]), ("Dokdo", ["400"]), ("Cute Font", ["400"]),
            ("Gothic A1", ["300", "400", "700", "900"]),
            ("Noto Sans KR", ["100", "300", "400", "700", "900"])]


def pick(weights):
    """굵기가 많으면 골고루 MAX_WEIGHTS 개만 고른다. 400 은 항상 넣는다."""
    ws = sorted({str(w) for w in weights if str(w).isdigit()}, key=int)
    if not ws:
        return ["400"]
    if len(ws) <= MAX_WEIGHTS:
        return ws
    keep = {ws[0], ws[-1], "400" if "400" in ws else ws[len(ws) // 2]}
    for w in ws:                      # 남는 자리는 가벼운 것부터 채운다
        if len(keep) >= MAX_WEIGHTS:
            break
        keep.add(w)
    return sorted(keep, key=int)


def families(c):
    try:
        raw = c.get(META).text
        d = json.loads(raw[raw.index("{"):])
        out = []
        for f in d["familyMetadataList"]:
            if "korean" in (f.get("subsets") or []):
                out.append((f["family"], pick((f.get("fonts") or {}).keys())))
        if out:
            print("구글 폰트 한글 패밀리 %d개" % len(out))
            return sorted(out)
    except Exception as e:
        print("목록 받기 실패(%s) — 기본 목록으로 진행" % str(e)[:40])
    return FALLBACK


ok = fail = skip = 0
with httpx.Client(headers=UA, timeout=60, follow_redirects=True) as c:
    for fam, weights in families(c):
        q = fam.replace(" ", "+")
        if weights and weights != ["400"]:
            q += ":wght@" + ";".join(weights)
        try:
            css = c.get("https://fonts.googleapis.com/css2?family=%s&display=swap" % q).text
        except Exception as e:
            print("  %-24s CSS 실패 %s" % (fam, str(e)[:40])); fail += 1; continue
        urls = sorted(set(re.findall(r"url\((https://[^)]+\.ttf)\)", css)))
        if not urls:
            print("  %-24s ttf 없음" % fam); fail += 1; continue
        for i, u in enumerate(urls):
            name = "%s%s.ttf" % (fam.replace(" ", ""),
                                 "" if len(urls) == 1 else "-%d" % (i + 1))
            p = OUT / name
            if p.exists() and p.stat().st_size > 10000:
                skip += 1; continue
            try:
                p.write_bytes(c.get(u).content)
                ok += 1
            except Exception as e:
                print("  %-24s 받기 실패 %s" % (fam, str(e)[:40])); fail += 1
        print("  %-24s %d개" % (fam, len(urls)))
        time.sleep(0.15)

files = list(OUT.glob("*.ttf"))
print("\n받음 %d · 건너뜀 %d · 실패 %d" % (ok, skip, fail))
print("총 %d개 파일 · %.1fMB" % (len(files), sum(f.stat().st_size for f in files) / 1e6))
