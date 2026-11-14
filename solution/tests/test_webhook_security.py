
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from solution.app import models
from solution.app.config import load_config
from solution.app.database import Base
from solution.app.models import (
    Campaign,
    Lead,
    Call,
    ActiveCallLock,
    PhoneSuppression,
    WebhookEvent,
)
from solution.app.security import hash_phone, encrypt_value
from solution.app.webhooks import process_call_completed


ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = ROOT / "assignment-b-workflow" / "fixtures" / "campaigns.json"


@pytest.fixture
def setup():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    TestSession = sessionmaker(bind=engine)

    config = load_config(CONFIG_PATH)

    phone = "+919876543210"
    phone_hash = hash_phone(phone)

    with TestSession() as session:
        session.add(
            Campaign(
                id="c_acme_renewals",
                tenant_id="acme_insurance",
                start_time="10:00",
                end_time="19:00",
                active_days="mon,tue,wed,thu,fri,sat",
                max_attempts=3,
            )
        )

        session.add(
            Lead(
                campaign_id="c_acme_renewals",
                lead_id="lead_001",
                phone_encrypted=encrypt_value(phone),
                phone_hash=phone_hash,
                customer_name_encrypted=encrypt_value("Test Customer"),
                attempts=1,
                status="calling",
            )
        )
        session.commit()

        session.add(
            Call(
                campaign_id="c_acme_renewals",
                lead_id="lead_001",
                request_id="request_001",
                provider_call_sid="CA123",
                attempt_number=1,
                status="active",
            )
        )

        session.add(
            ActiveCallLock(
                phone_hash=phone_hash,
                request_id="request_001",
                tenant_id="acme_insurance",
                campaign_id="c_acme_renewals",
                lead_id="lead_001",
                slot_number=1,
            )
        )
        session.commit()

        yield session, config, phone

    engine.dispose()


def make_payload(phone, status="no_answer", disposition=None):
    ended = datetime.now(timezone.utc)

    return {
        "event": "call.completed",
        "call_sid": "CA123",
        "request_id": "request_001",
        "tenant_id": "acme_insurance",
        "campaign_id": "c_acme_renewals",
        "lead_id": "lead_001",
        "phone": phone,
        "status": status,
        "disposition": disposition,
        "callback_at": None,
        "started_at": (ended - timedelta(minutes=2)).isoformat(),
        "ended_at": ended.isoformat(),
        "duration_sec": 120,
    }


def test_no_answer_sets_retry_time_and_releases_lock(setup):
    session, config, phone = setup
    payload = make_payload(phone)

    assert process_call_completed(session, payload, config) is True

    lead = session.get(Lead, ("c_acme_renewals", "lead_001"))
    call = session.query(Call).filter_by(request_id="request_001").one()

    assert lead.status == "retry_wait"
    assert lead.next_eligible_at is not None
    assert lead.last_disposition is None
    assert call.status == "no_answer"

    assert session.query(ActiveCallLock).count() == 0
    assert session.query(WebhookEvent).count() == 1


def test_duplicate_webhook_is_ignored(setup):
    session, config, phone = setup
    payload = make_payload(phone)

    assert process_call_completed(session, payload, config) is True
    assert process_call_completed(session, payload, config) is False

    assert session.query(WebhookEvent).count() == 1


def test_dnd_suppresses_phone(setup):
    session, config, phone = setup
    payload = make_payload(
        phone,
        status="completed",
        disposition="dnd",
    )

    assert process_call_completed(session, payload, config) is True

    lead = session.get(Lead, ("c_acme_renewals", "lead_001"))

    assert lead.status == "completed"
    assert session.query(PhoneSuppression).filter_by(
        phone_hash=hash_phone(phone)
    ).count() == 1


def test_terminal_disposition_completes_lead(setup):
    session, config, phone = setup
    payload = make_payload(
        phone,
        status="completed",
        disposition="interested",
    )

    assert process_call_completed(session, payload, config) is True

    lead = session.get(Lead, ("c_acme_renewals", "lead_001"))

    assert lead.status == "completed"
    assert lead.completed_at is not None
    assert lead.next_eligible_at is None


def test_wrong_phone_is_rejected(setup):
    session, config, phone = setup
    payload = make_payload("+919999999999")

    with pytest.raises(ValueError, match="phone"):
        process_call_completed(session, payload, config)

    session.rollback()

    lead = session.get(Lead, ("c_acme_renewals", "lead_001"))
    assert lead.status == "calling"


def test_callback_after_campaign_closing_moves_to_next_opening(setup):
    session, config, phone = setup

    payload = make_payload(
        phone,
        status="completed",
        disposition="callback_requested",
    )

    payload["callback_at"] = "2026-11-12T20:00:00+05:30"
    payload["ended_at"] = "2026-11-12T18:00:00+05:30"

    assert process_call_completed(session, payload, config) is True

    lead = session.get(Lead, ("c_acme_renewals", "lead_001"))

    assert lead.status == "callback_wait"
    assert lead.callback_at is not None
    assert lead.next_eligible_at is not None

    # The campaign opens at 10:00 AM the next permitted day.
    scheduled = lead.next_eligible_at
    if scheduled.tzinfo is None:
        scheduled = scheduled.replace(tzinfo=config.timezone)

    assert scheduled.hour == 10
    assert scheduled.minute == 0
    assert scheduled.day == 13


def test_callback_before_campaign_opening_moves_to_opening(setup):
    session, config, phone = setup

    payload = make_payload(
        phone,
        status="completed",
        disposition="callback_requested",
    )

    payload["callback_at"] = "2026-11-12T08:00:00+05:30"
    payload["ended_at"] = "2026-11-12T07:00:00+05:30"

    assert process_call_completed(session, payload, config) is True

    lead = session.get(Lead, ("c_acme_renewals", "lead_001"))

    scheduled = lead.next_eligible_at
    if scheduled.tzinfo is None:
        scheduled = scheduled.replace(tzinfo=config.timezone)

    assert lead.status == "callback_wait"
    assert scheduled.hour == 10
    assert scheduled.minute == 0
    assert scheduled.day == 12


def test_callback_on_non_working_day_moves_to_next_allowed_day(setup):
    session, config, phone = setup

    payload = make_payload(
        phone,
        status="completed",
        disposition="callback_requested",
    )

    # Sunday is not an allowed day for c_acme_renewals.
    payload["callback_at"] = "2026-11-15T12:00:00+05:30"
    payload["ended_at"] = "2026-11-14T18:00:00+05:30"

    assert process_call_completed(session, payload, config) is True

    lead = session.get(Lead, ("c_acme_renewals", "lead_001"))

    scheduled = lead.next_eligible_at
    if scheduled.tzinfo is None:
        scheduled = scheduled.replace(tzinfo=config.timezone)

    assert lead.status == "callback_wait"
    assert scheduled.weekday() == 0
    assert scheduled.hour == 10
    assert scheduled.minute == 0
    assert scheduled.day == 16
