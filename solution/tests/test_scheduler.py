
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from solution.app.config import load_config
from solution.app.database import Base
from solution.app.models import Campaign, Lead
from solution.app.scheduler import (
    get_eligible_leads,
    is_inside_window,
    next_campaign_open,
)
from solution.app.security import encrypt_value, hash_phone


ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = ROOT / "assignment-b-workflow" / "fixtures" / "campaigns.json"


@pytest.fixture
def scheduler_setup():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)

    config = load_config(CONFIG_PATH)

    with Session() as session:
        for campaign_id, campaign in config.campaigns.items():
            session.add(
                Campaign(
                    id=campaign_id,
                    tenant_id=campaign.tenant_id,
                    start_time=campaign.window_start,
                    end_time=campaign.window_end,
                    active_days=",".join(campaign.days),
                    max_attempts=campaign.max_attempts,
                    enabled=True,
                )
            )

        session.commit()

        yield session, config

    engine.dispose()


def add_lead(session, campaign_id, lead_id, phone, status="pending",
             attempts=0, callback_at=None, next_eligible_at=None):
    session.add(
        Lead(
            campaign_id=campaign_id,
            lead_id=lead_id,
            phone_encrypted=encrypt_value(phone),
            phone_hash=hash_phone(phone),
            customer_name_encrypted=encrypt_value("Test Customer"),
            status=status,
            attempts=attempts,
            callback_at=callback_at,
            next_eligible_at=next_eligible_at,
        )
    )
    session.commit()


def test_calling_window_uses_start_inclusive_end_exclusive():
    now = datetime.fromisoformat("2026-11-12T10:00:00+05:30")

    assert is_inside_window(now, "10:00", "19:00") is True

    end = datetime.fromisoformat("2026-11-12T19:00:00+05:30")

    assert is_inside_window(end, "10:00", "19:00") is False


def test_disallowed_day_is_rejected():
    sunday = datetime.fromisoformat("2026-11-15T12:00:00+05:30")

    assert is_inside_window(
        sunday,
        "10:00",
        "19:00",
        ("mon", "tue", "wed", "thu", "fri", "sat"),
    ) is False


def test_next_campaign_open_skips_closed_day():
    config = load_config(CONFIG_PATH)
    campaign = config.campaigns["c_zen_winback"]

    sunday = datetime.fromisoformat("2026-11-15T20:00:00+05:30")

    result = next_campaign_open(
        sunday,
        campaign,
        config.regulatory_start,
        config.regulatory_end,
    )

    assert result.weekday() == 0
    assert result.hour == 9
    assert result.minute == 30


def test_callbacks_are_prioritized_over_retries_and_fresh(scheduler_setup):
    session, config = scheduler_setup
    now = datetime.fromisoformat("2026-11-12T12:00:00+05:30")

    add_lead(
        session, "c_acme_renewals", "fresh_1",
        "+919876543201",
    )
    add_lead(
        session, "c_acme_renewals", "retry_1",
        "+919876543202",
        status="retry_wait",
        attempts=1,
        next_eligible_at=now - timedelta(minutes=5),
    )
    add_lead(
        session, "c_acme_renewals", "callback_1",
        "+919876543203",
        status="callback_wait",
        attempts=1,
        callback_at=now - timedelta(minutes=5),
    )

    result = get_eligible_leads(session, config, now)

    assert [lead.lead_id for lead in result] == [
        "callback_1",
        "retry_1",
        "fresh_1",
    ]


def test_future_callback_is_not_eligible(scheduler_setup):
    session, config = scheduler_setup
    now = datetime.fromisoformat("2026-11-12T12:00:00+05:30")

    add_lead(
        session,
        "c_acme_renewals",
        "future_callback",
        "+919876543204",
        status="callback_wait",
        attempts=1,
        callback_at=now + timedelta(minutes=30),
    )

    assert get_eligible_leads(session, config, now) == []


def test_exhausted_lead_is_not_eligible(scheduler_setup):
    session, config = scheduler_setup

    campaign = config.campaigns["c_acme_renewals"]

    add_lead(
        session,
        "c_acme_renewals",
        "exhausted_lead",
        "+919876543205",
        status="pending",
        attempts=campaign.max_attempts,
    )

    now = datetime.fromisoformat("2026-11-12T12:00:00+05:30")

    assert get_eligible_leads(session, config, now) == []
