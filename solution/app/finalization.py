
from sqlalchemy.orm import Session

from solution.app.models import ActiveCallLock, Lead, PhoneSuppression


def finalize_leads(session: Session, config) -> dict:
    """
    Finalize leads that can no longer receive calls.

    Completed leads are never overwritten.
    Scheduled callbacks are preserved while attempts remain.
    Leads at the maximum attempt count become exhausted.
    Active calls and phone locks prevent finalization.
    """
    counts = {
        "completed": 0,
        "exhausted": 0,
        "dnd_blocked": 0,
        "unchanged": 0,
    }

    leads = session.query(Lead).all()

    try:
        for lead in leads:
            if lead.status == "completed":
                counts["unchanged"] += 1
                continue

            campaign_config = config.campaigns.get(lead.campaign_id)

            if campaign_config is None:
                counts["unchanged"] += 1
                continue

            active_lock = (
                session.query(ActiveCallLock)
                .filter_by(phone_hash=lead.phone_hash)
                .first()
            )

            if active_lock is not None or lead.status == "calling":
                counts["unchanged"] += 1
                continue

            # Preserve a callback only while another attempt is allowed.
            if (
                lead.status == "callback_wait"
                and lead.attempts < campaign_config.max_attempts
            ):
                counts["unchanged"] += 1
                continue

            suppression = (
                session.query(PhoneSuppression)
                .filter_by(phone_hash=lead.phone_hash)
                .first()
            )

            # Exhaustion takes precedence over DND suppression.
            if lead.attempts >= campaign_config.max_attempts:
                new_status = "exhausted"
            elif suppression is not None:
                new_status = "dnd_blocked"
            else:
                counts["unchanged"] += 1
                continue

            if lead.status == new_status:
                counts["unchanged"] += 1
                continue

            lead.status = new_status
            lead.next_eligible_at = None
            lead.callback_at = None
            lead.completed_at = None

            counts[new_status] += 1

        session.commit()
        return counts

    except Exception:
        session.rollback()
        raise
