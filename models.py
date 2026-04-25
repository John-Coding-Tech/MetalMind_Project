"""
models.py — SQLAlchemy models.
"""

from datetime import datetime, timezone
from sqlalchemy import Column, Index, Integer, String, Float, Text, DateTime, Date, JSON, ForeignKey, Boolean
from sqlalchemy.orm import relationship
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
    deep_report = Column(JSON, nullable=True)
    report_generated_at = Column(DateTime, nullable=True)
    saved_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    # Soft-delete flag. When a user "unsaves" a supplier we flip this to
    # False instead of removing the row, so their manually entered Details
    # (rating/pros/cons/quoted_price/moq/etc.) survive for re-activation.
    is_saved = Column(Boolean, default=True, nullable=False, index=True)

    # === User Primary Decision Data ===
    # Tier 1: Quick assessment
    decision_stage = Column(String(50), nullable=True)
    rating = Column(Integer, nullable=True)
    tags = Column(JSON, nullable=True)
    pros = Column(JSON, nullable=True)
    cons = Column(JSON, nullable=True)

    # Tier 2: Commercial facts
    quoted_price = Column(Float, nullable=True)
    quoted_currency = Column(String(3), nullable=True)
    quoted_unit = Column(String(20), nullable=True)
    moq = Column(Integer, nullable=True)
    lead_time_days = Column(Integer, nullable=True)
    payment_terms = Column(Text, nullable=True)
    incoterms = Column(String(10), nullable=True)

    # Tier 3: Trust & product verification
    sample_status = Column(String(30), nullable=True)
    sample_quality = Column(Integer, nullable=True)
    factory_verified_via = Column(JSON, nullable=True)
    # Legacy narrow columns kept for backwards compat (old data); use reference_1/2/3 for new writes.
    coating_confirmed = Column(String(20), nullable=True)
    core_material_confirmed = Column(String(20), nullable=True)
    fire_rating_confirmed = Column(String(10), nullable=True)
    # Free-text reference links / notes (replaces the mislabelled legacy columns above)
    reference_1 = Column(Text, nullable=True)
    reference_2 = Column(Text, nullable=True)
    reference_3 = Column(Text, nullable=True)
    warranty_years = Column(Integer, nullable=True)
    next_action_date = Column(Date, nullable=True)

    attachments = relationship(
        "SupplierAttachment",
        back_populates="supplier",
        cascade="all, delete-orphan",
    )

    __table_args__ = (
        Index("ix_supplier_name_url", "supplier_name", "url"),
    )


class SupplierAttachment(Base):
    """Files uploaded by the user for a specific saved supplier.
    Stored on the local filesystem; only metadata lives in the DB.
    The path is a project-root-relative string (e.g. "uploads/12/7_quote.pdf").
    """
    __tablename__ = "supplier_attachments"

    id          = Column(Integer, primary_key=True, index=True)
    supplier_id = Column(Integer, ForeignKey("saved_suppliers.id", ondelete="CASCADE"),
                         nullable=False, index=True)
    filename    = Column(String(500), nullable=False)   # original user filename
    stored_path = Column(String(1000), nullable=False)  # relative path on disk
    mime_type   = Column(String(150), nullable=True)
    size_bytes  = Column(Integer, nullable=True)
    uploaded_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    supplier = relationship("SavedSupplier", back_populates="attachments")
