
from datetime import datetime
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from solution.app.config import load_config
from solution.app.database import Base
from solution.app.finalization import finalize_leads
from solution.app.models import (
    Campaign,
    Lead,
    ActiveCallLock,
    PhoneSuppression,
)
from solution.app.security import encrypt_value, hash_phone


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
        campaign_config = config.campaigns["c_acme_renewals"]

        session.add(
            Campaign(
                id="c_acme_renewals",
                tenant_id=campaign_config.tenant_id,
                start_time=campaign_config.window_start,
                end_time=campaign_config.window_end,
                active_days=",".join(campaign_config.days),
                max_attempts=campaign_config.max_attempts,
            )
        )

        session.add(
            Lead(
                campaign_id="c_acme_renewals",
                lead_id="lead_001",
                phone_encrypted=encrypt_value(phone),
                phone_hash=phone_hash,
                customer_name_encrypted=encrypt_value("Test Customer"),
                attempts=0,
                status="pending",
            )
        )
        session.commit()

        yield session, config, phone_hash

    engine.dispose()


def add_suppression(session, phone_hash):
    session.add(
        PhoneSuppression(
            phone_hash=phone_hash,
            reason="dnd",
            source_campaign_id="c_acme_renewals",
        )
    )
    session.commit()


def test_max_attempts_marks_lead_exhausted(setup):
    session, config, _ = setup
    lead = session.get(Lead, ("c_acme_renewals", "lead_001"))
    lead.attempts = config.campaigns["c_acme_renewals"].max_attempts
    session.commit()

    result = finalize_leads(session, config)

    session.refresh(lead)
    assert lead.status == "exhausted"
    assert result["exhausted"] == 1


def test_global_suppression_marks_lead_dnd_blocked(setup):
    session, config, phone_hash = setup
    lead = session.get(Lead, ("c_acme_renewals", "lead_001"))

    add_suppression(session, phone_hash)

    result = finalize_leads(session, config)

    session.refresh(lead)
    assert lead.status == "dnd_blocked"
    assert result["dnd_blocked"] == 1


def test_completed_lead_is_never_overwritten_by_dnd(setup):
    session, config, phone_hash = setup
    lead = session.get(Lead, ("c_acme_renewals", "lead_001"))
    lead.status = "completed"
    session.commit()

    add_suppression(session, phone_hash)

    finalize_leads(session, config)

    session.refresh(lead)
    assert lead.status == "completed"


def test_exhausted_takes_precedence_over_dnd(setup):
    session, config, phone_hash = setup
    lead = session.get(Lead, ("c_acme_renewals", "lead_001"))
    lead.attempts = config.campaigns["c_acme_renewals"].max_attempts
    session.commit()

    add_suppression(session, phone_hash)

    finalize_leads(session, config)

    session.refresh(lead)
    assert lead.status == "exhausted"


def test_active_phone_lock_prevents_finalization(setup):
    session, config, phone_hash = setup
    lead = session.get(Lead, ("c_acme_renewals", "lead_001"))
    lead.attempts = config.campaigns["c_acme_renewals"].max_attempts

    session.add(
        ActiveCallLock(
            phone_hash=phone_hash,
            request_id="active_request_001",
            tenant_id="acme_insurance",
            campaign_id="c_acme_renewals",
            lead_id="lead_001",
            slot_number=1,
        )
    )
    session.commit()

    finalize_leads(session, config)

    session.refresh(lead)
    assert lead.status == "pending"


def test_callback_is_preserved_when_attempts_remain(setup):
    session, config, _ = setup
    lead = session.get(Lead, ("c_acme_renewals", "lead_001"))

    lead.status = "callback_wait"
    lead.attempts = 2
    lead.callback_at = datetime.fromisoformat(
        "2026-11-14T15:00:00+05:30"
    )
    lead.next_eligible_at = lead.callback_at
    session.commit()

    finalize_leads(session, config)

    session.refresh(lead)

    assert lead.status == "callback_wait"
    assert lead.callback_at is not None
    assert lead.next_eligible_at is not None


def test_callback_at_max_attempts_becomes_exhausted(setup):
    session, config, _ = setup
    lead = session.get(Lead, ("c_acme_renewals", "lead_001"))

    lead.status = "callback_wait"
    lead.attempts = config.campaigns["c_acme_renewals"].max_attempts
    lead.callback_at = datetime.fromisoformat(
        "2026-11-14T15:00:00+05:30"
    )
    lead.next_eligible_at = lead.callback_at
    session.commit()

    finalize_leads(session, config)

    session.refresh(lead)

    assert lead.status == "exhausted"
    assert lead.callback_at is None
    assert lead.next_eligible_at is None
