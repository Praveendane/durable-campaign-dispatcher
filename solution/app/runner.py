
import argparse
import csv
import logging
import time
from pathlib import Path

import httpx

from solution.app.config import load_config
from solution.app.database import SessionLocal, create_tables
from solution.app.dispatcher import dispatch_once, PROVIDER_URL
from solution.app.finalization import finalize_leads, FINAL_STATES
from solution.app.importer import import_campaign_data
from solution.app.models import ActiveCallLock, Call, Lead


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[2]

CONFIG_PATH = (
    PROJECT_ROOT
    / "assignment-b-workflow"
    / "fixtures"
    / "campaigns.json"
)

FIXTURES_DIR = CONFIG_PATH.parent
OUTPUT_PATH = PROJECT_ROOT / "final_state.csv"

RECOVERABLE_STATUSES = ("submitting", "uncertain", "active")

CSV_FIELDS = [
    "campaign_id",
    "lead_id",
    "final_state",
    "attempts",
    "last_status",
    "last_disposition",
]


def import_fixtures(config):
    """Create database tables and safely import campaign leads."""
    create_tables()

    session = SessionLocal()

    try:
        result = import_campaign_data(
            session,
            config,
            FIXTURES_DIR,
        )

        logger.info("Fixture import: %s", result)
        return result

    finally:
        session.close()


def check_provider_completion(client):
    """Read the provider's authoritative lead and activity reports."""
    leads_response = client.get("/admin/leads")
    leads_response.raise_for_status()

    report_response = client.get("/admin/report")
    report_response.raise_for_status()

    provider_leads = leads_response.json()
    provider_report = report_response.json()

    all_final = (
        bool(provider_leads)
        and all(
            lead["expected_state"] in FINAL_STATES
            for lead in provider_leads
        )
    )

    no_active_calls = provider_report["calls_active"] == 0

    return (
        all_final and no_active_calls,
        provider_leads,
        provider_report,
    )


def export_final_state(session, provider_leads):
    """
    Export final lead states after validating them against the provider.

    Calls without a provider_call_sid were never accepted and must not
    be treated as the provider's last call.
    """
    provider_by_key = {
        (item["campaign_id"], item["lead_id"]): item
        for item in provider_leads
    }

    local_leads = session.query(Lead).all()

    if len(local_leads) != len(provider_by_key):
        raise RuntimeError(
            "Local and provider lead counts do not match."
        )

    rows = []

    for lead in local_leads:
        key = (lead.campaign_id, lead.lead_id)
        expected = provider_by_key.get(key)

        if expected is None:
            raise RuntimeError(
                f"Lead missing from provider report: {key}"
            )

        if lead.status != expected["expected_state"]:
            raise RuntimeError(
                f"Final-state mismatch for {key}: "
                f"local={lead.status}, "
                f"provider={expected['expected_state']}"
            )

        if lead.attempts != expected["attempts"]:
            raise RuntimeError(
                f"Attempt-count mismatch for {key}: "
                f"local={lead.attempts}, "
                f"provider={expected['attempts']}"
            )

        # Ignore locally recorded requests that were never accepted.
        last_call = (
            session.query(Call)
            .filter(
                Call.campaign_id == lead.campaign_id,
                Call.lead_id == lead.lead_id,
                Call.provider_call_sid.isnot(None),
            )
            .order_by(Call.created_at.desc())
            .first()
        )

        local_last_status = (
            last_call.status if last_call else ""
        )

        local_last_disposition = (
            (last_call.disposition or "")
            if last_call
            else ""
        )

        if local_last_status != expected["last_status"]:
            raise RuntimeError(
                f"Last-status mismatch for {key}: "
                f"local={local_last_status!r}, "
                f"provider={expected['last_status']!r}"
            )

        if local_last_disposition != expected["last_disposition"]:
            raise RuntimeError(
                f"Last-disposition mismatch for {key}: "
                f"local={local_last_disposition!r}, "
                f"provider={expected['last_disposition']!r}"
            )

        rows.append({
            "campaign_id": lead.campaign_id,
            "lead_id": lead.lead_id,
            "final_state": lead.status,
            "attempts": lead.attempts,
            "last_status": local_last_status,
            "last_disposition": local_last_disposition,
        })

    rows.sort(
        key=lambda row: (
            row["campaign_id"],
            row["lead_id"],
        )
    )

    with OUTPUT_PATH.open(
        "w",
        newline="",
        encoding="utf-8",
    ) as file:
        writer = csv.DictWriter(
            file,
            fieldnames=CSV_FIELDS,
        )

        writer.writeheader()
        writer.writerows(rows)

    logger.info(
        "Final CSV exported successfully: %s (%s leads)",
        OUTPUT_PATH,
        len(rows),
    )


def local_completion_ready(session):
    """Ensure the local database has no unfinished work."""
    unfinished_leads = (
        session.query(Lead)
        .filter(~Lead.status.in_(FINAL_STATES))
        .count()
    )

    recoverable_calls = (
        session.query(Call)
        .filter(Call.status.in_(RECOVERABLE_STATUSES))
        .count()
    )

    active_locks = session.query(ActiveCallLock).count()

    return (
        unfinished_leads == 0
        and recoverable_calls == 0
        and active_locks == 0
    )


def run_dispatcher(poll_interval=0.5, limit=100):
    """Continuously dispatch, recover, finalize, and verify campaigns."""
    config = load_config(CONFIG_PATH)

    import_fixtures(config)

    logger.info("Starting durable campaign dispatcher.")
    logger.info("Provider URL: %s", PROVIDER_URL)

    with httpx.Client(
        base_url=PROVIDER_URL,
        timeout=15.0,
    ) as provider_client:

        while True:
            session = SessionLocal()

            try:
                # Reconcile unfinished calls before submitting new ones.
                results = dispatch_once(
                    session,
                    config,
                    limit=limit,
                )

                if results:
                    logger.info(
                        "Dispatch cycle processed %s requests.",
                        len(results),
                    )

                # Finalize leads that cannot receive another call.
                finalize_leads(session, config)

                # Check the provider's authoritative final state.
                (
                    provider_done,
                    provider_leads,
                    report,
                ) = check_provider_completion(provider_client)

                if (
                    provider_done
                    and local_completion_ready(session)
                ):
                    export_final_state(session, provider_leads)

                    logger.info(
                        "Campaign processing completed. "
                        "Provider violations: %s",
                        report["violation_count"],
                    )

                    if report["violation_count"] != 0:
                        logger.error(
                            "Provider reported calling-rule violations."
                        )

                    return

            except httpx.HTTPError:
                logger.exception(
                    "Provider communication failed. Retrying."
                )

            except Exception:
                logger.exception(
                    "Dispatcher cycle failed. Retrying."
                )

            finally:
                session.close()

            time.sleep(poll_interval)


def main():
    parser = argparse.ArgumentParser(
        description="Durable Campaign Dispatcher"
    )

    parser.add_argument(
        "--poll-interval",
        type=float,
        default=0.5,
        help="Seconds between dispatch cycles.",
    )

    parser.add_argument(
        "--limit",
        type=int,
        default=100,
        help="Maximum eligible leads per dispatch cycle.",
    )

    args = parser.parse_args()

    if args.poll_interval <= 0 or args.limit <= 0:
        parser.error(
            "--poll-interval and --limit must be positive."
        )

    run_dispatcher(
        poll_interval=args.poll_interval,
        limit=args.limit,
    )


if __name__ == "__main__":
    main()
