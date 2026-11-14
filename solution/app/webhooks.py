
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from solution.app.models import (
    ActiveCallLock,
    Call,
    Campaign,
    Lead,
    PhoneSuppression,
    WebhookEvent,
)
from solution.app.security import hash_phone
from solution.app.scheduler import next_campaign_open


TERMINAL_DISPOSITIONS = {
    "interested",
    "not_interested",
    "wrong_number",
    "dnd",
}

VALID_STATUSES = {
    "no_answer",
    "busy",
    "failed",
    "completed",
}


def parse_datetime(value):
    """Parse an ISO datetime and ensure it is timezone-aware."""
    if not value:
        return None

    parsed = datetime.fromisoformat(value)

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)

    return parsed


def process_call_completed(
    session: Session,
    payload: dict,
    config,
) -> bool:
    """
    Process one verified call.completed webhook.

    Returns:
        True: event processed successfully.
        False: duplicate event already processed.

    Raises:
        ValueError: invalid or mismatched webhook data.
    """
    if payload.get("event") != "call.completed":
        raise ValueError("Unsupported webhook event")

    call_sid = payload.get("call_sid")
    request_id = payload.get("request_id")
    campaign_id = payload.get("campaign_id")
    lead_id = payload.get("lead_id")

    if not all((call_sid, request_id, campaign_id, lead_id)):
        raise ValueError("Webhook is missing required identifiers")

    event_key = f"{call_sid}:call.completed"

    try:
        existing_event = (
            session.query(WebhookEvent)
            .filter_by(event_key=event_key)
            .first()
        )

        if (
            existing_event
            and existing_event.processing_status == "processed"
        ):
            return False

        call = (
            session.query(Call)
            .filter_by(request_id=request_id)
            .with_for_update()
            .first()
        )

        if call is None:
            raise ValueError("Unknown request_id")

        if (
            call.campaign_id != campaign_id
            or call.lead_id != lead_id
        ):
            raise ValueError("Webhook does not match stored call")

        if (
            call.provider_call_sid
            and call.provider_call_sid != call_sid
        ):
            raise ValueError("Provider call SID mismatch")

        lead = (
            session.query(Lead)
            .filter_by(
                campaign_id=campaign_id,
                lead_id=lead_id,
            )
            .with_for_update()
            .first()
        )

        if lead is None:
            raise ValueError("Unknown lead")

        campaign = (
            session.query(Campaign)
            .filter_by(id=campaign_id)
            .first()
        )

        if campaign is None:
            raise ValueError("Unknown campaign")

        if payload.get("tenant_id") != campaign.tenant_id:
            raise ValueError("Webhook tenant does not match campaign")

        phone = payload.get("phone")

        if not phone:
            raise ValueError("Webhook is missing phone")

        if hash_phone(phone) != lead.phone_hash:
            raise ValueError("Webhook phone does not match stored lead")

        status = payload.get("status")
        disposition = payload.get("disposition")

        if status not in VALID_STATUSES:
            raise ValueError("Unsupported call status")

        if status == "completed":
            valid_dispositions = TERMINAL_DISPOSITIONS | {
                "callback_requested"
            }

            if disposition not in valid_dispositions:
                raise ValueError(
                    "Unsupported completed-call disposition"
                )

        ended_at = parse_datetime(payload.get("ended_at"))
        started_at = parse_datetime(payload.get("started_at"))
        callback_at = parse_datetime(payload.get("callback_at"))

        if ended_at is None:
            raise ValueError("Webhook is missing ended_at")

        if (
            disposition == "callback_requested"
            and callback_at is None
        ):
            raise ValueError("Callback event is missing callback_at")

        if existing_event is None:
            event = WebhookEvent(
                event_key=event_key,
                provider_call_sid=call_sid,
                processing_status="received",
            )
            session.add(event)
        else:
            event = existing_event

        # Update the provider call record.
        call.provider_call_sid = call_sid
        call.status = status
        call.disposition = disposition
        call.started_at = started_at
        call.ended_at = ended_at

        # Update the lead's latest result.
        lead.last_disposition = disposition

        if disposition in TERMINAL_DISPOSITIONS:
            lead.status = "completed"
            lead.completed_at = ended_at
            lead.next_eligible_at = None
            lead.callback_at = None

        elif disposition == "callback_requested":
            lead.status = "callback_wait"

            # Preserve the customer's original requested callback time.
            lead.callback_at = callback_at

            # Move the callback to the next valid campaign window
            # when the requested time falls outside permitted hours/days.
            campaign_config = config.campaigns[campaign_id]

            lead.next_eligible_at = next_campaign_open(
                callback_at,
                campaign_config,
                config.regulatory_start,
                config.regulatory_end,
            )

        elif status in {"no_answer", "busy", "failed"}:
            retry_minutes = getattr(
                config.retry_policy,
                f"{status}_minutes",
            )

            lead.callback_at = None

            if (
                lead.attempts
                >= config.campaigns[campaign_id].max_attempts
            ):
                lead.status = "exhausted"
                lead.next_eligible_at = None
            else:
                lead.status = "retry_wait"
                lead.next_eligible_at = (
                    ended_at + timedelta(minutes=retry_minutes)
                )

        # Persist global DND suppression.
        if disposition == "dnd":
            suppression = (
                session.query(PhoneSuppression)
                .filter_by(phone_hash=lead.phone_hash)
                .first()
            )

            if suppression is None:
                session.add(
                    PhoneSuppression(
                        phone_hash=lead.phone_hash,
                        reason="dnd",
                        source_campaign_id=campaign_id,
                    )
                )

            # Block every other eligible lead with this phone.
            affected_leads = (
                session.query(Lead)
                .filter(
                    Lead.phone_hash == lead.phone_hash,
                    Lead.status.notin_(
                        ["completed", "exhausted"]
                    ),
                )
                .all()
            )

            for affected in affected_leads:
                if (
                    affected.campaign_id == campaign_id
                    and affected.lead_id == lead_id
                ):
                    continue

                affected.status = "dnd_blocked"
                affected.next_eligible_at = None
                affected.callback_at = None

        # Release only the lock belonging to this request.
        lock = (
            session.query(ActiveCallLock)
            .filter_by(request_id=request_id)
            .first()
        )

        if lock is not None:
            session.delete(lock)

        event.processing_status = "processed"
        event.processed_at = datetime.now(timezone.utc)

        session.commit()
        return True

    except Exception:
        session.rollback()
        raise
