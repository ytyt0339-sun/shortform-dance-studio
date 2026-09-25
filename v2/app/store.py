# -*- coding: utf-8 -*-
"""파일을 어디에 둘지 한 곳에서 정한다.

지금은 서버 디스크에 그대로 쌓는다. 서버가 한 대일 때는 문제가 없지만,
두 대로 늘리거나 서버가 매번 새로 뜨는 곳(Lambda, Cloud Run)에 올리면
한쪽이 만든 파일을 다른 쪽이 못 본다.

그래서 읽고 쓰는 길을 이 파일 하나로 모았다. 환경변수 STORE 로 고른다.

  STORE=local  (기본)  서버 디스크에 둔다. 지금까지와 똑같다.
  STORE=s3             S3 나 Cloudflare R2 에 둔다. 서버가 몇 대든 같은 걸 본다.

ffmpeg 과 mediapipe 는 진짜 파일이 있어야 돌아가므로, s3 를 쓸 때도
작업 폴더는 그대로 쓴다. 다만 만든 결과를 올리고, 없으면 내려받아 채운다.
"""
import os
import shutil
from pathlib import Path

KIND = (os.environ.get("STORE") or "local").lower()
BUCKET = os.environ.get("S3_BUCKET") or ""
PREFIX = (os.environ.get("S3_PREFIX") or "runs").strip("/")
_S3 = None


def _s3():
    global _S3
    if _S3 is None:
        import boto3
        from botocore.config import Config
        # 요즘 boto3 는 올릴 때 체크섬을 끼워 보내는데, 구글 저장소(GCS)의
        # S3 호환 창구는 그걸 모르는 값이라며 400 을 낸다. 꼭 필요할 때만 쓰게 한다.
        try:
            cfg = Config(request_checksum_calculation="when_required",
                         response_checksum_validation="when_required")
        except TypeError:
            cfg = Config()            # 오래된 botocore 는 이 설정이 없다
        # 람다 안에서는 AWS_ACCESS_KEY_ID 같은 이름을 쓸 수 없다(예약어라 막힌다).
        # 그래서 R2 열쇠는 S3_KEY / S3_SECRET 이라는 이름으로 받는다.
        extra = {}
        if os.environ.get("S3_KEY"):
            extra["aws_access_key_id"] = os.environ["S3_KEY"]
            extra["aws_secret_access_key"] = os.environ.get("S3_SECRET") or ""
        _S3 = boto3.client(
            "s3",
            endpoint_url=os.environ.get("S3_ENDPOINT") or None,   # R2·GCS 는 여기를 채운다
            region_name=os.environ.get("S3_REGION") or None,
            config=cfg,
            **extra
        )
    return _S3


def _key(rel):
    return "%s/%s" % (PREFIX, str(rel).replace("\\", "/").lstrip("/"))


def enabled():
    """멀리 있는 저장소를 쓰는 중인가."""
    return KIND == "s3" and bool(BUCKET)


def put(local_path, rel):
    """만든 파일을 저장소에 올린다. local 이면 아무것도 하지 않는다."""
    if not enabled():
        return False
    _s3().upload_file(str(local_path), BUCKET, _key(rel))
    return True


def get(rel, local_path):
    """저장소에서 내려받아 작업 폴더를 채운다. 이미 있으면 그대로 둔다."""
    p = Path(local_path)
    if p.exists() and p.stat().st_size > 0:
        return True
    if not enabled():
        return False
    p.parent.mkdir(parents=True, exist_ok=True)
    try:
        _s3().download_file(BUCKET, _key(rel), str(p))
        return True
    except Exception:
        return False


def read_json(rel, default=None):
    """저장소에 있는 작은 기록을 곧바로 읽는다.

    서버가 여러 대일 때 하루 한도 같은 숫자는 내 디스크에 남은 헌 사본이 아니라
    저장소의 지금 값을 봐야 한다. (local 모드면 None 을 돌려준다)
    """
    if not enabled():
        return default
    import json as _json
    try:
        r = _s3().get_object(Bucket=BUCKET, Key=_key(rel))
        return _json.loads(r["Body"].read().decode("utf-8"))
    except Exception:
        return default


def exists(rel, local_path=None):
    if local_path and Path(local_path).exists():
        return True
    if not enabled():
        return False
    try:
        _s3().head_object(Bucket=BUCKET, Key=_key(rel))
        return True
    except Exception:
        return False


def link(rel, seconds=3600, filename=None, inline=True):
    """바로 내려받을 수 있는 임시 주소. 큰 영상은 서버를 거치지 않는 편이 낫다.

    filename 을 주면 저장할 때 그 이름이 된다 (내려받기 버튼용).
    """
    if not enabled():
        return None
    params = {"Bucket": BUCKET, "Key": _key(rel)}
    if filename:
        kind = "inline" if inline else "attachment"
        params["ResponseContentDisposition"] = '%s; filename="%s"' % (kind, filename)
    try:
        return _s3().generate_presigned_url("get_object", Params=params, ExpiresIn=seconds)
    except Exception:
        return None


def drop(rel_prefix):
    """작업을 지울 때 저장소 쪽도 함께 지운다."""
    if not enabled():
        return 0
    c = _s3()
    n = 0
    token = None
    while True:
        kw = {"Bucket": BUCKET, "Prefix": _key(rel_prefix)}
        if token:
            kw["ContinuationToken"] = token
        r = c.list_objects_v2(**kw)
        keys = [{"Key": o["Key"]} for o in r.get("Contents", [])]
        if keys:
            c.delete_objects(Bucket=BUCKET, Delete={"Objects": keys})
            n += len(keys)
        if not r.get("IsTruncated"):
            break
        token = r.get("NextContinuationToken")
    return n


def list_keys(rel_prefix):
    """저장소에 있는 파일 목록. (작업 폴더 안의 상대 경로로 돌려준다)"""
    if not enabled():
        return []
    c = _s3()
    pre = _key(rel_prefix)
    out, token = [], None
    while True:
        kw = {"Bucket": BUCKET, "Prefix": pre}
        if token:
            kw["ContinuationToken"] = token
        r = c.list_objects_v2(**kw)
        for o in r.get("Contents", []):
            out.append(o["Key"][len(_key("")):])
        if not r.get("IsTruncated"):
            return out
        token = r.get("NextContinuationToken")


def sync_down(rel_prefix, local_dir, only=None):
    """작업 폴더를 저장소에서 통째로 내려받는다.

    람다는 매번 빈 손으로 깨어난다. 일을 시작하기 전에 필요한 파일을 받아 온다.
    only 를 주면 그 이름들만 받는다 (큰 영상을 괜히 받지 않도록).
    """
    if not enabled():
        return 0
    d = Path(local_dir)
    n = 0
    c = _s3()
    for rel in list_keys(rel_prefix):
        name = rel[len(str(rel_prefix).rstrip("/")) + 1:]
        if only is not None and name not in only:
            continue
        p = d / name
        if p.exists() and p.stat().st_size > 0:
            # 같은 이름으로 다시 만들어진 파일일 수 있다. 크기가 같을 때만 건너뛴다.
            try:
                if c.head_object(Bucket=BUCKET, Key=_key(rel))["ContentLength"] == p.stat().st_size:
                    continue
            except Exception:
                continue
        p.parent.mkdir(parents=True, exist_ok=True)
        try:
            _s3().download_file(BUCKET, _key(rel), str(p))
            n += 1
        except Exception as e:
            print("내려받기 실패:", rel, str(e)[:80])
    return n


def snapshot(local_dir):
    """지금 폴더 상태를 적어 둔다. 일이 끝난 뒤 무엇이 새로 생겼는지 알려고."""
    d = Path(local_dir)
    out = {}
    for f in d.rglob("*"):
        if f.is_file():
            st = f.stat()
            out[str(f.relative_to(d)).replace("\\", "/")] = (st.st_mtime_ns, st.st_size)
    return out


def sync_up(local_dir, rel_prefix, before=None):
    """일하면서 새로 생기거나 바뀐 파일만 저장소로 올린다."""
    if not enabled():
        return []
    d = Path(local_dir)
    before = before or {}
    sent = []
    for f in sorted(d.rglob("*")):
        if not f.is_file():
            continue
        rel = str(f.relative_to(d)).replace("\\", "/")
        st = f.stat()
        if before.get(rel) == (st.st_mtime_ns, st.st_size):
            continue
        try:
            put(f, "%s/%s" % (str(rel_prefix).rstrip("/"), rel))
            sent.append(rel)
        except Exception as e:
            print("올리기 실패:", rel, str(e)[:80])
    return sent


def copy_in(src, local_path, rel):
    """올라온 파일을 작업 폴더에 두고 저장소에도 올린다."""
    p = Path(local_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    if str(src) != str(p):
        shutil.copy(str(src), str(p))
    put(p, rel)
    return p
