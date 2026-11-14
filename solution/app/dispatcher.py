
import logging
import os
import uuid
from datetime import datetime

import httpx
from sqlalchemy.orm import Session

from solution.app.models import (
    ActiveCallLock,
    Call,
    Lead,
)
from solution.app.security import decrypt_value
from solution.app.scheduler import get_eligible_leads
from solution.app.locks import (
    reserve_active_call_lock,
    release_active_call_lock,
)
from solution.app.webhooks import process_call_completed


logger = logging.getLogger(__name__)

PROVIDER_URL = os.getenv(
    "TELEPHONY_PROVIDER_URL",
    "http://127.0.0.1:8099",
)

CALLBACK_URL = os.getenv(
    "DISPATCHER_CALLBACK_URL",
    "http://127.0.0.1:8000/webhooks/provider",
)

# Include active calls because their completion webhooks may be lost.
RECOVERABLE_STATUSES = (
    "submitting",
    "uncertain",
    "active",
)

WEBHOOK_STATUSES = {
    "no_answer",
    "busy",
    "failed",
    "completed",
}


def get_provider_clock(client: httpx.Client) -> datetime:
    """Read simulated time from the provider."""
    response = client.get("/v1/clock")
    response.raise_for_status()

    return datetime.fromisoformat(response.json()["sim_now"])


def find_provider_call(client, request_id: str):
    """Find a previously submitted request using its original ID."""
    response = client.get(
        "/v1/calls",
        params={"request_id": request_id},
    )

    if response.status_code == 404:
        return None

    response.raise_for_status()
    return response.json()


def mark_not_placed(
    session: Session,
    call: Call,
    request_id: str,
    reason: str,
) -> None:
    """Record a confirmed rejection without counting an attempt."""
    call.status = "not_placed"
    call.disposition = reason

    release_active_call_lock(session, request_id)
    session.commit()


def record_accepted_call(
    session: Session,
    call: Call,
    lead: Lead,
    provider_result: dict,
) -> None:
    """Persist acceptance without overwriting an early webhook."""

    call_sid = provider_result.get("call_sid")

    if not call_sid:
        raise ValueError(
            "Provider accepted the request without returning call_sid"
        )

    if call.provider_call_sid and call.provider_call_sid != call_sid:
        raise ValueError("Provider call SID mismatch")

    call.provider_call_sid = call_sid

    if call.status not in WEBHOOK_STATUSES:
        call.status = provider_result.get("state", "active")

    # Use the attempt number stored with this durable request.
    if lead.attempts < call.attempt_number:
        lead.attempts = call.attempt_number

    if call.status not in WEBHOOK_STATUSES:
        lead.status = "calling"

    session.commit()


def ensure_active_lock(
    session: Session,
    call: Call,
    lead: Lead,
    config,
) -> bool:
    """Ensure an existing request still owns its active-call lock."""

    existing = (
        session.query(ActiveCallLock)
        .filter_by(request_id=call.request_id)
        .first()
    )

    if existing is not None:
        return True

    campaign_config = config.campaigns[call.campaign_id]
    tenant_config = config.tenants[campaign_config.tenant_id]

    return reserve_active_call_lock(
        session,
        phone_hash=lead.phone_hash,
        request_id=call.request_id,
        tenant_id=campaign_config.tenant_id,
        campaign_id=call.campaign_id,
        lead_id=call.lead_id,
        max_concurrent_calls=tenant_config.max_concurrent_calls,
    )


def build_call_payload(
    call: Call,
    lead: Lead,
) -> dict:
    """Build the provider request using the original durable ID."""

    return {
        "request_id": call.request_id,
        "campaign_id": call.campaign_id,
        "lead_id": call.lead_id,
        "phone": decrypt_value(lead.phone_encrypted),
        "callback_url": CALLBACK_URL,
    }


def process_provider_result(
    session: Session,
    call: Call,
    lead: Lead,
    provider_result: dict,
    config,
) -> None:
    """Recover an accepted call and any completed result."""

    record_accepted_call(
        session,
        call,
        lead,
        provider_result,
    )

    # The provider returns the completed webhook payload inside
    # "result" when the call has already ended.
    completed_result = provider_result.get("result")

    if completed_result:
        process_call_completed(
            session,
            completed_result,
            config,
        )


def reconcile_call(
    session: Session,
    client: httpx.Client,
    config,
    call: Call,
) -> dict:
    """
    Reconcile one previously persisted request.

    Never create a new request ID during reconciliation.
    """

    lead = (
        session.query(Lead)
        .filter_by(
            campaign_id=call.campaign_id,
            lead_id=call.lead_id,
        )
        .first()
    )

    if lead is None:
        logger.error(
            "Cannot reconcile request %s: lead not found",
            call.request_id,
        )
        return {
            "request_id": call.request_id,
            "status": "uncertain",
            "reason": "lead_not_found",
        }

    try:
        provider_result = find_provider_call(
            client,
            call.request_id,
        )

        if provider_result is not None:
            process_provider_result(
                session,
                call,
                lead,
                provider_result,
                config,
            )

            return {
                "request_id": call.request_id,
                "call_sid": call.provider_call_sid,
                "status": call.status,
                "reconciled": True,
            }

        # A provider 404 confirms this request is not currently
        # registered. Reuse the original request ID.
        if not ensure_active_lock(
            session,
            call,
            lead,
            config,
        ):
            call.status = "uncertain"
            session.commit()

            return {
                "request_id": call.request_id,
                "status": "uncertain",
                "reason": "lock_unavailable",
            }

        payload = build_call_payload(call, lead)

        response = client.post(
            "/v1/calls",
            json=payload,
        )

        if response.status_code in (429, 503):
            mark_not_placed(
                session,
                call,
                call.request_id,
                f"provider_{response.status_code}",
            )

            return {
                "request_id": call.request_id,
                "status": "not_placed",
            }

        if response.status_code >= 500:
            call.status = "uncertain"
            session.commit()

            return {
                "request_id": call.request_id,
                "status": "uncertain",
            }

        response.raise_for_status()

        process_provider_result(
            session,
            call,
            lead,
            response.json(),
            config,
        )

        return {
            "request_id": call.request_id,
            "call_sid": call.provider_call_sid,
            "status": call.status,
            "reconciled": True,
        }

    except (httpx.TimeoutException, httpx.NetworkError):
        call.status = "uncertain"
        session.commit()

        logger.warning(
            "Reconciliation remains uncertain for %s",
            call.request_id,
            exc_info=True,
        )

        return {
            "request_id": call.request_id,
            "status": "uncertain",
        }

    except httpx.HTTPStatusError as exc:
        status_code = exc.response.status_code

        if status_code >= 500:
            call.status = "uncertain"
            session.commit()

            return {
                "request_id": call.request_id,
                "status": "uncertain",
            }

        mark_not_placed(
            session,
            call,
            call.request_id,
            f"provider_http_{status_code}",
        )

        return {
            "request_id": call.request_id,
            "status": "not_placed",
        }

    except Exception:
        session.rollback()

        logger.exception(
            "Reconciliation failed for request %s",
            call.request_id,
        )
        raise


def reconcile_pending_calls(
    session: Session,
    client: httpx.Client,
    config,
) -> list[dict]:
    """
    Recover requests left unfinished by a restart or lost webhook.

    Includes submitting, uncertain, and active calls.
    """

    pending_calls = (
        session.query(Call)
        .filter(Call.status.in_(RECOVERABLE_STATUSES))
        .order_by(Call.created_at.asc())
        .all()
    )

    results = []

    for call in pending_calls:
        results.append(
            reconcile_call(
                session,
                client,
                config,
                call,
            )
        )

    return results


def submit_call(
    session: Session,
    client: httpx.Client,
    config,
    lead: Lead,
) -> dict:
    """Persist, lock, and submit one new call."""

    campaign_config = config.campaigns[lead.campaign_id]
    tenant_config = config.tenants[campaign_config.tenant_id]

    request_id = str(uuid.uuid4())

    call = Call(
        campaign_id=lead.campaign_id,
        lead_id=lead.lead_id,
        request_id=request_id,
        attempt_number=lead.attempts + 1,
        status="submitting",
    )

    # Persist the request before contacting the provider.
    session.add(call)
    session.commit()

    reserved = reserve_active_call_lock(
        session,
        phone_hash=lead.phone_hash,
        request_id=request_id,
        tenant_id=campaign_config.tenant_id,
        campaign_id=lead.campaign_id,
        lead_id=lead.lead_id,
        max_concurrent_calls=tenant_config.max_concurrent_calls,
    )

    if not reserved:
        call.status = "not_placed"
        call.disposition = "capacity_or_phone_lock_unavailable"
        session.commit()

        return {
            "request_id": request_id,
            "status": "not_placed",
            "reason": "capacity_or_phone_lock_unavailable",
        }

    try:
        payload = build_call_payload(call, lead)

        response = client.post(
            "/v1/calls",
            json=payload,
        )

        if response.status_code in (429, 503):
            mark_not_placed(
                session,
                call,
                request_id,
                f"provider_{response.status_code}",
            )

            return {
                "request_id": request_id,
                "status": "not_placed",
            }

        if response.status_code == 504:
            provider_result = find_provider_call(
                client,
                request_id,
            )

            if provider_result is None:
                # Retry once using the same idempotency key.
                response = client.post(
                    "/v1/calls",
                    json=payload,
                )

                if response.status_code in (429, 503):
                    mark_not_placed(
                        session,
                        call,
                        request_id,
                        f"provider_{response.status_code}",
                    )

                    return {
                        "request_id": request_id,
                        "status": "not_placed",
                    }

                if response.status_code >= 500:
                    call.status = "uncertain"
                    session.commit()

                    return {
                        "request_id": request_id,
                        "status": "uncertain",
                    }

                response.raise_for_status()
                provider_result = response.json()

        else:
            if response.status_code >= 500:
                call.status = "uncertain"
                session.commit()

                return {
                    "request_id": request_id,
                    "status": "uncertain",
                }

            response.raise_for_status()
            provider_result = response.json()

        process_provider_result(
            session,
            call,
            lead,
            provider_result,
            config,
        )

        return {
            "request_id": request_id,
            "call_sid": call.provider_call_sid,
            "status": call.status,
            "campaign_id": lead.campaign_id,
            "lead_id": lead.lead_id,
        }

    except httpx.HTTPStatusError as exc:
        status_code = exc.response.status_code

        if status_code >= 500:
            call.status = "uncertain"
            session.commit()

            return {
                "request_id": request_id,
                "status": "uncertain",
            }

        mark_not_placed(
            session,
            call,
            request_id,
            f"provider_http_{status_code}",
        )

        return {
            "request_id": request_id,
            "status": "not_placed",
        }

    except (httpx.TimeoutException, httpx.NetworkError):
        call.status = "uncertain"
        session.commit()

        logger.warning(
            "Uncertain provider submission: %s",
            request_id,
            exc_info=True,
        )

        return {
            "request_id": request_id,
            "status": "uncertain",
        }

    except Exception:
        session.rollback()

        logger.exception(
            "Unexpected dispatcher error for request %s",
            request_id,
        )
        raise



def dispatch_once(
    session: Session,
    config,
    limit: int = 100,
) -> list[dict]:
    """
    Reconcile old requests first, then dispatch eligible leads.

    Refresh provider simulated time before every submission so a
    batch cannot continue dialing after a campaign window closes.
    """

    results = []

    with httpx.Client(
        base_url=PROVIDER_URL,
        timeout=15.0,
    ) as client:

        # Recover unfinished requests before dispatching fresh leads.
        recovery_results = reconcile_pending_calls(
            session,
            client,
            config,
        )

        results.extend(recovery_results)

        # Re-evaluate eligibility using a fresh provider clock
        # before every individual call.
        for _ in range(limit):
            sim_now = get_provider_clock(client)

            eligible_leads = get_eligible_leads(
                session,
                config,
                sim_now,
                limit=1,
            )

            if not eligible_leads:
                break

            lead = eligible_leads[0]

            # Submit only the lead found eligible at this
            # latest simulated time.
            result = submit_call(
                session,
                client,
                config,
                lead,
            )

            results.append(result)

    return results
