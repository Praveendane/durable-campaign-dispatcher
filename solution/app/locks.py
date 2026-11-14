from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from solution.app.models import ActiveCallLock


def reserve_active_call_lock(
    session: Session,
    *,
    phone_hash: str,
    request_id: str,
    tenant_id: str,
    campaign_id: str,
    lead_id: str,
    max_concurrent_calls: int,
) -> bool:
    """
    Atomically reserve a global phone lock and one tenant capacity slot.

    Returns False if the phone is already active or tenant capacity is full.
    """
    if max_concurrent_calls < 1:
        return False

    for slot in range(1, max_concurrent_calls + 1):
        lock = ActiveCallLock(
            phone_hash=phone_hash,
            request_id=request_id,
            tenant_id=tenant_id,
            campaign_id=campaign_id,
            lead_id=lead_id,
            slot_number=slot,
        )

        session.add(lock)

        try:
            session.commit()
            return True

        except IntegrityError:
            session.rollback()

            # A phone already reserved by another campaign cannot be retried.
            existing_phone = (
                session.query(ActiveCallLock)
                .filter_by(phone_hash=phone_hash)
                .first()
            )

            if existing_phone:
                return False

            # Otherwise, this tenant slot was occupied. Try the next slot.

    return False


def release_active_call_lock(
    session: Session,
    request_id: str,
) -> bool:
    """Release the phone and tenant slot belonging to a completed call."""
    lock = (
        session.query(ActiveCallLock)
        .filter_by(request_id=request_id)
        .first()
    )

    if lock is None:
        return False

    session.delete(lock)
    session.commit()
    return True