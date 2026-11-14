
from datetime import datetime, timezone

from sqlalchemy import (
    Column,
    String,
    Integer,
    DateTime,
    Boolean,
    ForeignKeyConstraint,
    Index,
    UniqueConstraint,
)

from solution.app.database import Base


def utc_now():
    return datetime.now(timezone.utc)


class Campaign(Base):
    __tablename__ = "campaigns"

    id = Column(String, primary_key=True)
    tenant_id = Column(String, nullable=False, index=True)
    start_time = Column(String, nullable=False)
    end_time = Column(String, nullable=False)
    active_days = Column(String, nullable=False)
    max_attempts = Column(Integer, nullable=False, default=3)
    enabled = Column(Boolean, nullable=False, default=True)


class Lead(Base):
    __tablename__ = "leads"

    campaign_id = Column(
        String,
        primary_key=True,
    )
    lead_id = Column(
        String,
        primary_key=True,
    )

    phone_encrypted = Column(String, nullable=False)
    phone_hash = Column(String, nullable=False, index=True)
    customer_name_encrypted = Column(String, nullable=False)

    attempts = Column(Integer, nullable=False, default=0)
    status = Column(String, nullable=False, default="pending")
    last_disposition = Column(String, nullable=True)

    next_eligible_at = Column(DateTime(timezone=True), nullable=True)
    callback_at = Column(DateTime(timezone=True), nullable=True)
    completed_at = Column(DateTime(timezone=True), nullable=True)


class Call(Base):
    __tablename__ = "calls"

    id = Column(Integer, primary_key=True, autoincrement=True)

    campaign_id = Column(String, nullable=False)
    lead_id = Column(String, nullable=False)

    request_id = Column(String, nullable=False, unique=True)
    provider_call_sid = Column(String, nullable=True, unique=True)

    attempt_number = Column(Integer, nullable=False)
    status = Column(String, nullable=False, default="pending")
    disposition = Column(String, nullable=True)

    created_at = Column(DateTime(timezone=True), nullable=False, default=utc_now)
    started_at = Column(DateTime(timezone=True), nullable=True)
    ended_at = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        ForeignKeyConstraint(
            ["campaign_id", "lead_id"],
            ["leads.campaign_id", "leads.lead_id"],
        ),
    )


class WebhookEvent(Base):
    __tablename__ = "webhook_events"

    id = Column(Integer, primary_key=True, autoincrement=True)

    event_key = Column(String, nullable=False, unique=True)
    provider_call_sid = Column(String, nullable=False, index=True)

    received_at = Column(DateTime(timezone=True), nullable=False, default=utc_now)
    processed_at = Column(DateTime(timezone=True), nullable=True)

    processing_status = Column(
        String,
        nullable=False,
        default="received",
    )


class PhoneSuppression(Base):
    __tablename__ = "phone_suppressions"

    phone_hash = Column(String, primary_key=True)
    reason = Column(String, nullable=False)
    source_campaign_id = Column(String, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utc_now)

class ActiveCallLock(Base):
    __tablename__ = "active_call_locks"

    phone_hash = Column(String, primary_key=True)
    request_id = Column(String, nullable=False, unique=True)

    tenant_id = Column(String, nullable=False)
    campaign_id = Column(String, nullable=False)
    lead_id = Column(String, nullable=False)

    slot_number = Column(Integer, nullable=False)

    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
    )

    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "slot_number",
            name="uq_tenant_active_slot",
        ),
    )