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
        _S3 = boto3.client(
            "s3",
            endpoint_url=os.environ.get("S3_ENDPOINT") or None,   # R2 는 여기를 채운다
            region_name=os.environ.get("S3_REGION") or None,
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


def copy_in(src, local_path, rel):
    """올라온 파일을 작업 폴더에 두고 저장소에도 올린다."""
    p = Path(local_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    if str(src) != str(p):
        shutil.copy(str(src), str(p))
    put(p, rel)
    return p
