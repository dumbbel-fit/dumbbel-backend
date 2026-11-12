import ssl
from contextlib import contextmanager
from urllib.parse import parse_qs, urlparse, unquote

from sqlalchemy import create_engine
from sqlalchemy.engine import URL
from sqlalchemy.orm import DeclarativeBase, sessionmaker, Session

from app.config import settings


def _build_engine():
    u = urlparse(settings.database_url)
    qs = parse_qs(u.query)
    url = URL.create(
        "mysql+pymysql",
        username=unquote(u.username or ""),
        password=unquote(u.password or "") or None,
        host=u.hostname,
        port=u.port or 3306,
        database=(u.path or "/").lstrip("/") or None,
        query={"charset": "utf8mb4"},
    )
    connect_args: dict = {"init_command": "SET time_zone = '+00:00'"}
    ssl_mode = (qs.get("ssl-mode") or qs.get("ssl_mode") or [""])[0].upper()
    if ssl_mode in ("REQUIRED", "VERIFY_CA", "VERIFY_IDENTITY") or settings.db_ssl_ca:
        if settings.db_ssl_ca:
            ctx = ssl.create_default_context(cafile=settings.db_ssl_ca)
            ctx.check_hostname = ssl_mode == "VERIFY_IDENTITY"
        else:
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        connect_args["ssl"] = ctx
    return create_engine(
        url,
        pool_size=settings.db_pool_size,
        max_overflow=10,
        pool_pre_ping=True,
        pool_recycle=1800,
        connect_args=connect_args,
        future=True,
    )


engine = _build_engine()
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@contextmanager
def session_scope() -> Session:
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
