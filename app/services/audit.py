from datetime import date, datetime

from sqlalchemy.orm import Session

from app.models import AuditLog
from app.utils import utcnow


def _jsonable(v):
    if isinstance(v, (date, datetime)):
        return v.isoformat()
    if isinstance(v, dict):
        return {k: _jsonable(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_jsonable(x) for x in v]
    return v


def diff(before: dict, after: dict) -> tuple[dict, dict]:
    """Changed fields only."""
    b, a = {}, {}
    for k in set(before) | set(after):
        if before.get(k) != after.get(k):
            b[k], a[k] = before.get(k), after.get(k)
    return _jsonable(b), _jsonable(a)


def audit(db: Session, gym_id: str | None, actor_id: str | None, action: str, entity_type: str | None,
          entity_id: str | None, summary: str, before: dict | None = None, after: dict | None = None):
    """Added to the caller's transaction so it commits (or rolls back) with the write."""
    db.add(AuditLog(gym_id=gym_id, actor_user_id=actor_id, action=action, entity_type=entity_type,
                    entity_id=entity_id, summary=summary, before=_jsonable(before) if before else None,
                    after=_jsonable(after) if after else None, at=utcnow()))
