"""Tiny, additive auto-migration (safe on a live DB): creates missing tables, adds missing columns,
and extends ENUMs with new values. It never drops or renames anything."""
import logging
import re

from sqlalchemy import Enum, inspect, text
from sqlalchemy.schema import CreateColumn

from app.db import Base

log = logging.getLogger("dumbbel.migrate")


def _enum_values(column_type: str) -> list[str]:
    return re.findall(r"'((?:[^']|'')*)'", column_type)


def auto_migrate(engine):
    import app.models  # noqa: F401  (register tables)
    insp = inspect(engine)
    existing = set(insp.get_table_names())
    Base.metadata.create_all(engine)  # new tables only
    changed = [t.name for t in Base.metadata.sorted_tables if t.name not in existing]
    with engine.begin() as conn:
        db_name = conn.execute(text("SELECT DATABASE()")).scalar()
        for table in Base.metadata.sorted_tables:
            if table.name not in existing:
                continue
            db_cols = {r[0]: r[1] for r in conn.execute(text(
                "SELECT COLUMN_NAME, COLUMN_TYPE FROM information_schema.COLUMNS "
                "WHERE TABLE_SCHEMA=:s AND TABLE_NAME=:t"), {"s": db_name, "t": table.name})}
            for col in table.columns:
                ddl = str(CreateColumn(col).compile(dialect=engine.dialect)).strip()
                if col.name not in db_cols:
                    conn.execute(text(f"ALTER TABLE `{table.name}` ADD COLUMN {ddl}"))
                    changed.append(f"{table.name}.{col.name}")
                elif isinstance(col.type, Enum):
                    have = _enum_values(db_cols[col.name])
                    missing = [v for v in col.type.enums if v not in have]
                    if missing:
                        conn.execute(text(f"ALTER TABLE `{table.name}` MODIFY COLUMN {ddl}"))
                        changed.append(f"{table.name}.{col.name} +{missing}")
    if changed:
        log.info("schema migrated: %s", ", ".join(changed))
    return changed
