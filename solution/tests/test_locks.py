from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from solution.app.database import Base
from solution.app import models
from solution.app.locks import (
    reserve_active_call_lock,
    release_active_call_lock,
)


def setup_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)

    Session = sessionmaker(bind=engine)
    return Session()


def reserve(session, phone, request, tenant="tenant_a", campaign="campaign_a"):
    return reserve_active_call_lock(
        session,
        phone_hash=phone,
        request_id=request,
        tenant_id=tenant,
        campaign_id=campaign,
        lead_id="lead_" + request,
        max_concurrent_calls=2,
    )


def test_same_phone_cannot_be_reserved_twice_across_campaigns():
    session = setup_session()

    assert reserve(session, "phone_hash_1", "request_1") is True

    assert reserve(
        session,
        "phone_hash_1",
        "request_2",
        campaign="campaign_b",
    ) is False


def test_tenant_capacity_is_enforced():
    session = setup_session()

    assert reserve(session, "phone_1", "request_1") is True
    assert reserve(session, "phone_2", "request_2") is True
    assert reserve(session, "phone_3", "request_3") is False


def test_releasing_lock_frees_tenant_slot():
    session = setup_session()

    assert reserve(session, "phone_1", "request_1") is True
    assert reserve(session, "phone_2", "request_2") is True

    assert release_active_call_lock(session, "request_1") is True

    assert reserve(session, "phone_3", "request_3") is True


def test_releasing_unknown_lock_returns_false():
    session = setup_session()

    assert release_active_call_lock(session, "unknown_request") is False