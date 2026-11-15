"""Object storage: S3/R2/GCS when `S3_BUCKET` is set; otherwise files live in the database (`stored_files`) and are
served by this API through HMAC-signed URLs. (Local disk is only a read fallback — Cloud Run's disk is wiped on
every deploy, which is why photos used to disappear.)"""
import hashlib
import hmac
import os
import time
from pathlib import Path

from app.config import settings
from app.context import request_base_var


def _base() -> str:
    """Public origin for file links. PUBLIC_BASE_URL wins unless it's still the localhost default, in which case the
    address this request arrived on is used (so links work on Cloud Run even if the env var was dropped)."""
    cfg = (settings.public_base_url or "").rstrip("/")
    req = request_base_var.get()
    if req and (not cfg or "localhost" in cfg or "127.0.0.1" in cfg) and "localhost" not in req and "127.0.0.1" not in req:
        return req.rstrip("/")
    return cfg or (req or "").rstrip("/")

PRIVATE_PREFIXES = ("gym_document/", "import_file/")


def _s3():
    import boto3
    return boto3.client("s3", region_name=settings.s3_region, endpoint_url=settings.s3_endpoint_url,
                        aws_access_key_id=settings.s3_access_key, aws_secret_access_key=settings.s3_secret_key)


def use_s3() -> bool:
    return bool(settings.s3_bucket)


def bucket_for(key: str) -> str:
    if key.startswith(PRIVATE_PREFIXES):
        return settings.s3_private_bucket or settings.s3_bucket
    return settings.s3_bucket


def _sign(method: str, key: str, exp: int) -> str:
    return hmac.new(settings.jwt_secret.encode(), f"{method}:{key}:{exp}".encode(), hashlib.sha256).hexdigest()


def verify_local_sig(method: str, key: str, exp: int, sig: str) -> bool:
    return exp >= time.time() and hmac.compare_digest(_sign(method, key, exp), sig)


def local_path(key: str) -> Path:
    p = (Path(settings.local_storage_dir) / key).resolve()
    root = Path(settings.local_storage_dir).resolve()
    if root not in p.parents:
        raise ValueError("bad key")
    return p


def presign_put(key: str, content_type: str, expires: int = 600) -> str:
    if use_s3():
        return _s3().generate_presigned_url("put_object", Params={"Bucket": bucket_for(key), "Key": key,
                                                                  "ContentType": content_type}, ExpiresIn=expires)
    exp = int(time.time()) + expires
    return f"{_base()}{settings.api_prefix}/uploads/local/{key}?exp={exp}&sig={_sign('PUT', key, exp)}"


def signed_get_url(key: str | None, expires: int = 300, stable: bool = False) -> str | None:
    if not key:
        return None
    if key.startswith("http://") or key.startswith("https://"):
        return key
    if use_s3():
        return _s3().generate_presigned_url("get_object", Params={"Bucket": bucket_for(key), "Key": key},
                                            ExpiresIn=expires)
    exp = int(time.time()) + expires
    if stable:  # same URL for the whole day → browsers cache photos instead of re-downloading on every screen
        exp = (exp // 86400 + 1) * 86400
    return f"{_base()}{settings.api_prefix}/files/{key}?exp={exp}&sig={_sign('GET', key, exp)}"


def public_url(key: str | None) -> str | None:
    """Photos & logos. Signed for 24h so it works for both public and private buckets."""
    return signed_get_url(key, 86400, stable=True) if key else None


def _db():
    from app.db import session_scope
    return session_scope()


def put_bytes(key: str, data: bytes, content_type: str = "application/octet-stream"):
    if use_s3():
        _s3().put_object(Bucket=bucket_for(key), Key=key, Body=data, ContentType=content_type)
        return
    from app.models import StoredFile
    with _db() as db:
        row = db.get(StoredFile, key)
        if row:
            row.data, row.size, row.content_type = data, len(data), content_type
        else:
            db.add(StoredFile(key=key, data=data, size=len(data), content_type=content_type))


def get_bytes(key: str) -> bytes:
    if use_s3():
        return _s3().get_object(Bucket=bucket_for(key), Key=key)["Body"].read()
    from app.models import StoredFile
    with _db() as db:
        row = db.get(StoredFile, key)
        if row:
            return bytes(row.data)
    return local_path(key).read_bytes()  # files saved on disk before the database store existed


def exists(key: str) -> bool:
    try:
        if use_s3():
            _s3().head_object(Bucket=bucket_for(key), Key=key)
            return True
        from sqlalchemy import select
        from app.models import StoredFile
        with _db() as db:
            if db.scalar(select(StoredFile.key).where(StoredFile.key == key)):
                return True
        return local_path(key).exists()
    except Exception:
        return False


def delete(key: str):
    try:
        if use_s3():
            _s3().delete_object(Bucket=bucket_for(key), Key=key)
            return
        from app.models import StoredFile
        with _db() as db:
            row = db.get(StoredFile, key)
            if row:
                db.delete(row)
    except Exception:
        pass
    try:
        os.remove(local_path(key))
    except Exception:
        pass


def existing_keys(keys) -> set[str]:
    """Which of these keys still have a file behind them (one query). With S3 we assume all exist."""
    keys = [k for k in keys if k]
    if not keys or use_s3():
        return set(keys)
    from sqlalchemy import select
    from app.models import StoredFile
    with _db() as db:
        found = set(db.scalars(select(StoredFile.key).where(StoredFile.key.in_(keys))))
    for k in keys:
        if k not in found and not k.startswith(("http://", "https://")):
            try:
                if local_path(k).exists():
                    found.add(k)
            except Exception:
                pass
        elif k.startswith(("http://", "https://")):
            found.add(k)
    return found
