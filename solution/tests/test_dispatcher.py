
from pathlib import Path

import httpx
import pytest

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from solution.app.database import Base
from solution.app.config import load_config
from solution.app.models import Lead, Call, ActiveCallLock
from solution.app import dispatcher


ROOT = Path(__file__).resolve().parents[2]

CONFIG_PATH = (
    ROOT / "assignment-b-workflow" / "fixtures" / "campaigns.json"
)


class FakeResponse:
    def __init__(self, status_code, payload=None):
        self.status_code = status_code
        self.payload = payload or {}

    def json(self):
        return self.payload

    def raise_for_status(self):
        if self.status_code >= 400:
            request = httpx.Request("POST", "http://provider.test")
            response = httpx.Response(
                self.status_code,
                request=request,
            )
            raise httpx.HTTPStatusError(
                "Provider error",
                request=request,
                response=response,
            )


class FakeProvider:
    def __init__(self, post_responses, get_responses=None):
        self.post_responses = list(post_responses)
        self.get_responses = list(get_responses or [])
        self.post_payloads = []
        self.get_requests = []

    def post(self, path, json):
        self.post_payloads.append(json)
        result = self.post_responses.pop(0)

        if isinstance(result, Exception):
            raise result

        return result

    def get(self, path, params=None):
        self.get_requests.append((path, params))
        return self.get_responses.pop(0)


@pytest.fixture
def dispatcher_setup(monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)

    TestSession = sessionmaker(bind=engine)
    session = TestSession()

    config = load_config(CONFIG_PATH)

    lead = Lead(
        campaign_id="c_acme_renewals",
        lead_id="dispatcher_test_lead",
        phone_encrypted="encrypted_test_phone",
        phone_hash="dispatcher_test_phone_hash",
        customer_name_encrypted="encrypted_test_name",
        attempts=0,
        status="pending",
    )

    session.add(lead)
    session.commit()

    monkeypatch.setattr(
        dispatcher,
        "decrypt_value",
        lambda value: "+919876543210",
    )

    yield session, config, lead

    session.close()
    engine.dispose()


def test_successful_submission_counts_one_attempt(dispatcher_setup):
    session, config, lead = dispatcher_setup

    provider = FakeProvider([
        FakeResponse(
            200,
            {
                "call_sid": "CA_test_001",
                "state": "active",
            },
        )
    ])

    result = dispatcher.submit_call(
        session,
        provider,
        config,
        lead,
    )

    assert result["status"] == "active"
    assert result["call_sid"] == "CA_test_001"
    assert lead.attempts == 1
    assert lead.status == "calling"

    call = session.query(Call).one()

    assert call.request_id == result["request_id"]
    assert call.provider_call_sid == "CA_test_001"

    lock = session.query(ActiveCallLock).one()
    assert lock.request_id == result["request_id"]


def test_provider_503_does_not_count_attempt(dispatcher_setup):
    session, config, lead = dispatcher_setup

    provider = FakeProvider([
        FakeResponse(503)
    ])

    result = dispatcher.submit_call(
        session,
        provider,
        config,
        lead,
    )

    assert result["status"] == "not_placed"
    assert lead.attempts == 0

    call = session.query(Call).one()
    assert call.status == "not_placed"

    assert session.query(ActiveCallLock).count() == 0


def test_504_recovers_existing_provider_call(dispatcher_setup):
    session, config, lead = dispatcher_setup

    provider = FakeProvider(
        post_responses=[
            FakeResponse(504),
        ],
        get_responses=[
            FakeResponse(
                200,
                {
                    "call_sid": "CA_recovered_001",
                    "state": "active",
                },
            )
        ],
    )

    result = dispatcher.submit_call(
        session,
        provider,
        config,
        lead,
    )

    assert result["status"] == "active"
    assert result["call_sid"] == "CA_recovered_001"

    # The provider is queried using the original request ID.
    assert provider.get_requests[0][1]["request_id"] == (
        result["request_id"]
    )

    # No second POST is sent when the original call is found.
    assert len(provider.post_payloads) == 1

    assert lead.attempts == 1


def test_network_timeout_preserves_uncertain_request(dispatcher_setup):
    session, config, lead = dispatcher_setup

    timeout = httpx.ReadTimeout(
        "Provider response timed out"
    )

    provider = FakeProvider([
        timeout,
    ])

    result = dispatcher.submit_call(
        session,
        provider,
        config,
        lead,
    )

    assert result["status"] == "uncertain"
    assert lead.attempts == 0

    call = session.query(Call).one()
    assert call.status == "uncertain"

    # Keep the lock until reconciliation determines whether
    # the provider accepted the call.
    lock = session.query(ActiveCallLock).one()
    assert lock.request_id == result["request_id"]


def create_uncertain_call(session, config, lead):
    """Create a persisted uncertain request with its active lock."""

    campaign_config = config.campaigns[lead.campaign_id]
    tenant_config = config.tenants[campaign_config.tenant_id]

    call = Call(
        campaign_id=lead.campaign_id,
        lead_id=lead.lead_id,
        request_id="restart_recovery_request_001",
        attempt_number=1,
        status="uncertain",
    )

    session.add(call)
    session.commit()

    dispatcher.reserve_active_call_lock(
        session,
        phone_hash=lead.phone_hash,
        request_id=call.request_id,
        tenant_id=campaign_config.tenant_id,
        campaign_id=lead.campaign_id,
        lead_id=lead.lead_id,
        max_concurrent_calls=tenant_config.max_concurrent_calls,
    )

    return call


def test_restart_recovers_existing_provider_call(dispatcher_setup):
    session, config, lead = dispatcher_setup

    call = create_uncertain_call(session, config, lead)

    provider = FakeProvider(
        post_responses=[],
        get_responses=[
            FakeResponse(
                200,
                {
                    "call_sid": "CA_restart_recovered",
                    "request_id": call.request_id,
                    "state": "active",
                },
            )
        ],
    )

    results = dispatcher.reconcile_pending_calls(
        session,
        provider,
        config,
    )

    assert len(results) == 1
    assert results[0]["status"] == "active"
    assert results[0]["call_sid"] == "CA_restart_recovered"

    assert lead.attempts == 1
    assert call.provider_call_sid == "CA_restart_recovered"
    assert call.status == "active"

    # The active call must retain its lock.
    lock = session.query(ActiveCallLock).one()
    assert lock.request_id == call.request_id

    # Recovery must not submit a second POST.
    assert len(provider.post_payloads) == 0


def test_restart_reuses_original_request_id_after_provider_404(
    dispatcher_setup,
):
    session, config, lead = dispatcher_setup

    call = create_uncertain_call(session, config, lead)

    provider = FakeProvider(
        post_responses=[
            FakeResponse(
                201,
                {
                    "call_sid": "CA_restart_resubmitted",
                    "request_id": call.request_id,
                    "state": "active",
                },
            )
        ],
        get_responses=[
            FakeResponse(404),
        ],
    )

    results = dispatcher.reconcile_pending_calls(
        session,
        provider,
        config,
    )

    assert len(results) == 1
    assert results[0]["status"] == "active"

    # The original ID must be reused, never replaced.
    assert provider.post_payloads[0]["request_id"] == call.request_id

    assert lead.attempts == 1
    assert call.provider_call_sid == "CA_restart_resubmitted"
    assert call.status == "active"

    lock = session.query(ActiveCallLock).one()
    assert lock.request_id == call.request_id
