"""
models.py — SQLAlchemy models.
"""

from datetime import datetime, timezone
from sqlalchemy import Column, Integer, String, Float, Text, DateTime, JSON
from db import Base


class SavedSupplier(Base):
    __tablename__ = "saved_suppliers"

    id = Column(Integer, primary_key=True, index=True)
    supplier_name = Column(String(500), nullable=False)
    country = Column(String(100))
    price_display = Column(String(100))
    price_usd = Column(Float, nullable=True)
    risk_level = Column(String(50))
    risk_score = Column(Float, nullable=True)
    risk_reasons = Column(JSON, nullable=True)
    value_score = Column(Float, nullable=True)
    url = Column(String(1000))
    description = Column(Text, nullable=True)
    trust = Column(String(50), nullable=True)
    anomalies = Column(JSON, nullable=True)
    ai_adjustment = Column(JSON, nullable=True)
    notes = Column(Text, nullable=True)
    saved_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
