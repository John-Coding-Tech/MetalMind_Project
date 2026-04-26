"""
db.py — Database connection via SQLAlchemy.
Uses DATABASE_URL (Railway provides this). Falls back to SQLite for local dev.
"""

import os
import logging
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import OperationalError, ProgrammingError
from sqlalchemy.orm import sessionmaker, declarative_base

_log = logging.getLogger(__name__)

_env = os.environ.get("ENV", "development")
DATABASE_URL = os.environ.get("DATABASE_URL", "")
if not DATABASE_URL:
    if _env == "production":
        raise RuntimeError(
            "DATABASE_URL is not set. On Railway, add the Postgres plugin or set DATABASE_URL manually."
        )
    DATABASE_URL = "sqlite:///./metalmind.db"

if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

engine = create_engine(DATABASE_URL, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)
Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# Columns added after the initial schema. create_all won't add columns to
# existing tables, so we ALTER TABLE on startup for each missing one.
_SAVED_SUPPLIERS_NEW_COLUMNS = [
    ("decision_stage", "VARCHAR(50)"),
    ("rating", "INTEGER"),
    ("tags", "JSON"),
    ("pros", "JSON"),
    ("cons", "JSON"),
    ("quoted_price", "FLOAT"),
    ("quoted_currency", "VARCHAR(3)"),
    ("quoted_unit", "VARCHAR(20)"),
    ("moq", "INTEGER"),
    ("lead_time_days", "INTEGER"),
    ("payment_terms", "TEXT"),
    ("incoterms", "VARCHAR(10)"),
    ("sample_status", "VARCHAR(30)"),
    ("sample_quality", "INTEGER"),
    ("factory_verified_via", "JSON"),
    ("coating_confirmed", "VARCHAR(20)"),
    ("core_material_confirmed", "VARCHAR(20)"),
    ("fire_rating_confirmed", "VARCHAR(10)"),
    ("warranty_years", "INTEGER"),
    ("next_action_date", "DATE"),
    ("is_saved", "BOOLEAN NOT NULL DEFAULT TRUE"),
    ("reference_1", "TEXT"),
    ("reference_2", "TEXT"),
    ("reference_3", "TEXT"),
]


def _migrate_saved_suppliers():
    inspector = inspect(engine)
    if not inspector.has_table("saved_suppliers"):
        return
    existing = {c["name"] for c in inspector.get_columns("saved_suppliers")}
    for col_name, col_type in _SAVED_SUPPLIERS_NEW_COLUMNS:
        if col_name not in existing:
            try:
                with engine.begin() as conn:
                    conn.execute(text(f"ALTER TABLE saved_suppliers ADD COLUMN {col_name} {col_type}"))
                _log.info("Added column saved_suppliers.%s (%s)", col_name, col_type)
            except (OperationalError, ProgrammingError) as e:
                _log.warning("Failed to add column saved_suppliers.%s: %s", col_name, e)


def init_db():
    from models import SavedSupplier, SupplierAttachment  # noqa: F401
    Base.metadata.create_all(bind=engine)
    _migrate_saved_suppliers()
