# -*- coding: utf-8 -*-
"""AWS Lambda 손잡이.

무거운 일(자세 인식, 영상 자르고 붙이기)을 여기서 한다. 웹 쪽(Cloudflare)은
"이 작업의 이 일을 해줘" 하고 부르기만 한다.

람다는 매번 빈 손으로 깨어난다. 그래서 순서가 이렇다.
  1. 저장소(R2)에서 필요한 파일을 /tmp 로 내려받는다
  2. ops 의 함수를 부른다 — 서버가 쓰는 것과 **같은 코드**다
  3. 새로 생긴 파일을 저장소에 올리고, 고친 작업 기록을 돌려준다

부르는 쪽이 보내는 것
  {"op": "analyze_reference", "jid": "0922-1200-ab12",
   "params": {"name": "reference.mp4"}}
"""
import json
import os
import sys
import time
import traceback
from pathlib import Path

sys.path.insert(0, os.path.join(os.environ.get("LAMBDA_TASK_ROOT", "."), "app"))

import ops        # noqa: E402
import store      # noqa: E402

WORK = Path(os.environ.get("WORK_DIR") or "/tmp/jobs")
SAY_EVERY = 2.0          # 진행 상황을 저장소에 적는 간격(초). 너무 자주 적으면 느리다

# 작업과 상관없이 "알려주기만" 하는 것들. 작업 기록을 건드리지 않는다.
INFO_OPS = {"font_list"}


def _write_task(jid, task):
    """진행 상황을 작업 폴더와 저장소에 적는다. 화면은 이 파일만 본다."""
    try:
        p = WORK / jid / "task.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(task, ensure_ascii=False), encoding="utf-8")
        store.put(p, "%s/task.json" % jid)
    except Exception:
        pass          # 진행 표시를 못 적는다고 일을 멈출 이유는 없다


def _progress_writer(jid, box):
    """진행 상황을 작업 폴더의 task.json 으로 흘려보낸다.

    화면은 이 파일을 읽어 "지금 뭐 하는 중"을 보여준다. 람다가 도는 동안에도
    보이려면 중간중간 올려 줘야 한다.
    """
    last = [0.0]

    def say(msg=None, done=None, total=None):
        if msg is not None:
            box["msg"] = msg
        if done is not None:
            box["done"] = done
        if total is not None:
            box["total"] = total
        box["at"] = time.time()
        if time.time() - last[0] < SAY_EVERY:
            return
        last[0] = time.time()
        _write_task(jid, dict(box, state="running"))
    return say


def friendly(e):
    """사람이 읽을 수 있는 실패 문구. 화면에 그대로 뜬다."""
    s = str(e)
    if isinstance(e, FileNotFoundError):
        return "필요한 파일이 없습니다. 다시 올려 주세요."
    if "No complete upper body" in s or "upper body" in s:
        return "춤 영상에서 사람의 상반신이 또렷하게 안 보입니다. 다른 구간을 골라 주세요."
    if "422" in s:
        return "레퍼런스에 사람이 한 명만 나와야 합니다."
    if "timed out" in s.lower() or "timeout" in s.lower():
        return "시간이 너무 오래 걸려 멈췄습니다. 다시 시도해 주세요."
    return "%s: %s" % (type(e).__name__, s[:200])


def handler(event, context):
    op = event.get("op")
    jid = event.get("jid")
    params = event.get("params") or {}
    if not op or not jid:
        return {"ok": False, "error": "op 과 jid 가 있어야 합니다."}
    fn = getattr(ops, op, None)
    if fn is None or op.startswith("_"):
        return {"ok": False, "error": "모르는 작업입니다: %s" % op}

    d = WORK / jid
    d.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    if op in INFO_OPS:
        try:
            return {"ok": True, "result": fn(), "seconds": round(time.time() - t0, 1)}
        except Exception as e:
            traceback.print_exc()
            return {"ok": False, "error": "%s: %s" % (type(e).__name__, e)}
    try:
        # 1. 재료 받기. need 를 주면 그것만 받는다 (큰 파일을 괜히 안 받도록)
        got = store.sync_down(jid, d, only=event.get("need"))
        f = d / "job.json"
        if f.exists():
            j = json.loads(f.read_text(encoding="utf-8"))
        else:
            j = event.get("job") or {}

        # 재료가 없는데 시작하면 한참 뒤에 엉뚱한 자리에서 터진다.
        # 여기서 먼저 확인하고 알아들을 수 있는 말로 알려준다.
        want = params.get("name") or j.get(event.get("needs") or "")
        if want and not (d / want).exists():
            raise FileNotFoundError(want)

        # 2. 일하기 — 서버와 같은 함수
        before = store.snapshot(d)
        box = {"label": event.get("label") or op}
        j = fn(d, j, say=_progress_writer(jid, box), **params)

        # 3. 결과 돌려주기
        f.write_text(json.dumps(j, ensure_ascii=False, indent=2), encoding="utf-8")
        sent = store.sync_up(d, jid, before)
        store.put(f, "%s/job.json" % jid)
        # 맡겨놓고 돌아가는 방식에서는 부르는 쪽이 결과를 못 본다.
        # 그래서 끝났다는 표시도 여기서 직접 적는다 (화면이 이 파일을 본다).
        _write_task(jid, {"state": "done", "at": time.time(),
                          "label": event.get("label") or op})
        return {"ok": True, "job": j, "files": sent, "got": got,
                "seconds": round(time.time() - t0, 1)}
    except Exception as e:
        traceback.print_exc()
        msg = friendly(e)
        _write_task(jid, {"state": "error", "msg": msg, "at": time.time()})
        return {"ok": False, "error": msg, "seconds": round(time.time() - t0, 1)}
