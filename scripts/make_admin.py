"""Grant platform-admin (internal console) access to a phone number.
Usage:  .venv/bin/python -m scripts.make_admin 9849011234 ["Name"]"""
import sys

from sqlalchemy import select

from app.db import Base, engine, session_scope
from app.models import User
from app.utils import normalize_phone

if __name__ == "__main__":
    from app.migrate import auto_migrate
    auto_migrate(engine)
    phone = normalize_phone(sys.argv[1])
    with session_scope() as db:
        u = db.scalar(select(User).where(User.phone == phone))
        if not u:
            u = User(phone=phone, name=sys.argv[2] if len(sys.argv) > 2 else None)
            db.add(u)
        u.is_platform_admin = True
    print(f"{phone} is now a platform admin. Log in with app=console.")
