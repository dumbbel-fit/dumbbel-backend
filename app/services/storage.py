"""Object storage: S3/R2 when configured, else local disk with HMAC-signed URLs served by this API."""
import hashlib
import hmac
import os
import time
from pathlib import Path

from app.config import settings

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
    return f"{settings.public_base_url}{settings.api_prefix}/uploads/local/{key}?exp={exp}&sig={_sign('PUT', key, exp)}"


def signed_get_url(key: str | None, expires: int = 300) -> str | None:
    if not key:
        return None
    if key.startswith("http://") or key.startswith("https://"):
        return key
    if use_s3():
        return _s3().generate_presigned_url("get_object", Params={"Bucket": bucket_for(key), "Key": key},
                                            ExpiresIn=expires)
    exp = int(time.time()) + expires
    return f"{settings.public_base_url}{settings.api_prefix}/files/{key}?exp={exp}&sig={_sign('GET', key, exp)}"


def public_url(key: str | None) -> str | None:
    """Photos & logos. Signed for 24h so it works for both public and private buckets."""
    return signed_get_url(key, 86400) if key else None


def put_bytes(key: str, data: bytes, content_type: str = "application/octet-stream"):
    if use_s3():
        _s3().put_object(Bucket=bucket_for(key), Key=key, Body=data, ContentType=content_type)
        return
    p = local_path(key)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)


def get_bytes(key: str) -> bytes:
    if use_s3():
        return _s3().get_object(Bucket=bucket_for(key), Key=key)["Body"].read()
    return local_path(key).read_bytes()


def exists(key: str) -> bool:
    try:
        if use_s3():
            _s3().head_object(Bucket=bucket_for(key), Key=key)
            return True
        return local_path(key).exists()
    except Exception:
        return False


def delete(key: str):
    try:
        if use_s3():
            _s3().delete_object(Bucket=bucket_for(key), Key=key)
        else:
            os.remove(local_path(key))
    except Exception:
        pass
