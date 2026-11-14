
from datetime import datetime, date, time, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from .models import (
    Campaign,
    Lead,
    PhoneSuppression,
    ActiveCallLock,
)


DAY_NAMES = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


def normalize_datetime(
    value: Optional[datetime],
    tz=None,
) -> Optional[datetime]:
    """
    Normalize datetimes returned by SQLite.

    SQLite can remove timezone information when reading DateTime values.
    Naive values are interpreted in the supplied local timezone.
    """
    if value is None:
        return None

    if tz is None:
        tz = value.tzinfo or ZoneInfo("Asia/Kolkata")

    if value.tzinfo is None:
        return value.replace(tzinfo=tz)

    return value.astimezone(tz)


def is_inside_window(
    current_time: datetime,
    start_time: str,
    end_time: str,
    allowed_days=None,
) -> bool:
    """
    Check campaign/regulatory calling window.

    Start is inclusive and end is exclusive.
    If allowed_days is omitted, all seven days are allowed.
    """
    tz = current_time.tzinfo or ZoneInfo("Asia/Kolkata")
    local_now = normalize_datetime(current_time, tz)

    if local_now is None:
        return False

    if allowed_days is None:
        allowed_days = DAY_NAMES

    allowed_days = tuple(
        day.lower() for day in allowed_days
    )

    current_day = DAY_NAMES[local_now.weekday()]

    if current_day not in allowed_days:
        return False

    start = time.fromisoformat(start_time)
    end = time.fromisoformat(end_time)
    current_clock = local_now.time().replace(tzinfo=None)

    return start <= current_clock < end


def next_campaign_open(
    current_time: datetime,
    campaign,
    regulatory_start: str,
    regulatory_end: str,
) -> Optional[datetime]:
    """
    Find the next valid campaign opening time.

    The campaign window is intersected with the regulatory window.
    """
    tz = current_time.tzinfo or ZoneInfo("Asia/Kolkata")
    local_now = normalize_datetime(current_time, tz)

    if local_now is None:
        return None

    campaign_start = time.fromisoformat(campaign.window_start)
    campaign_end = time.fromisoformat(campaign.window_end)

    regulatory_open = time.fromisoformat(regulatory_start)
    regulatory_close = time.fromisoformat(regulatory_end)

    effective_start = max(campaign_start, regulatory_open)
    effective_end = min(campaign_end, regulatory_close)

    if effective_start >= effective_end:
        return None

    allowed_days = tuple(
        day.lower() for day in campaign.days
    )

    for offset in range(15):
        candidate_date = local_now.date() + timedelta(days=offset)

        if DAY_NAMES[candidate_date.weekday()] not in allowed_days:
            continue

        opening = datetime.combine(
            candidate_date,
            effective_start,
            tzinfo=tz,
        )

        closing = datetime.combine(
            candidate_date,
            effective_end,
            tzinfo=tz,
        )

        if local_now >= closing:
            continue

        if local_now <= opening:
            return opening

        if opening <= local_now < closing:
            return local_now

    return None


def get_eligible_leads(
    session: Session,
    config,
    sim_now: datetime,
    limit: int = 100,
) -> list[tuple[Campaign, Lead]]:
    """
    Return leads eligible for calling, ordered by priority:

    1. Callbacks
    2. Retries
    3. Fresh leads

    Enforces calling windows, attempt limits, DND suppression,
    and global active-phone locks.
    """
    sim_now = normalize_datetime(sim_now, config.timezone)

    if sim_now is None:
        return []

    eligible = []

    leads = (
        session.query(Lead)
        .join(Campaign, Campaign.id == Lead.campaign_id)
        .filter(Campaign.enabled.is_(True))
        .all()
    )

    for lead in leads:
        campaign_config = config.campaigns.get(lead.campaign_id)

        if campaign_config is None:
            continue

        if lead.status in {
            "completed",
            "exhausted",
            "dnd_blocked",
            "calling",
        }:
            continue

        if lead.attempts >= campaign_config.max_attempts:
            continue

        # A DND suppression applies across all campaigns.
        suppression = (
            session.query(PhoneSuppression)
            .filter_by(phone_hash=lead.phone_hash)
            .first()
        )

        if suppression is not None:
            continue

        # A phone number can have only one active call globally.
        active_lock = (
            session.query(ActiveCallLock)
            .filter_by(phone_hash=lead.phone_hash)
            .first()
        )

        if active_lock is not None:
            continue

        # Regulatory calling window.
        if not is_inside_window(
            sim_now,
            config.regulatory_start,
            config.regulatory_end,
        ):
            continue

        # Campaign calling window and permitted days.
        if not is_inside_window(
            sim_now,
            campaign_config.window_start,
            campaign_config.window_end,
            campaign_config.days,
        ):
            continue

        callback_at = normalize_datetime(
            lead.callback_at,
            config.timezone,
        )

        next_eligible_at = normalize_datetime(
            lead.next_eligible_at,
            config.timezone,
        )

        if lead.status == "callback_wait":
            due_time = next_eligible_at or callback_at

            if due_time is None or due_time > sim_now:
                continue

            priority = 0

        elif lead.status == "retry_wait":
            if (
                next_eligible_at is None
                or next_eligible_at > sim_now
            ):
                continue

            priority = 1

        elif lead.status in {"pending", "fresh", "new"}:
            if (
                next_eligible_at is not None
                and next_eligible_at > sim_now
            ):
                continue

            priority = 2

        else:
            continue

        scheduled_time = (
            next_eligible_at
            or callback_at
            or datetime.min.replace(tzinfo=config.timezone)
        )

        eligible.append(
            (priority, scheduled_time, campaign_config, lead)
        )

    eligible.sort(
        key=lambda item: (
            item[0],
            item[1],
            item[3].campaign_id,
            item[3].lead_id,
        )
    )

    return [
        lead
        for _priority, _scheduled_time, campaign, lead
        in eligible[:limit]
    ]
