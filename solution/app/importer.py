
import csv
from pathlib import Path

from sqlalchemy.orm import Session

from solution.app.models import Campaign, Lead
from solution.app.security import (
    encrypt_value,
    hash_phone,
    normalize_phone,
)


def import_campaign_data(
    session: Session,
    config,
    fixtures_dir: str | Path,
) -> dict:
    """
    Import campaign configurations and leads.
    Existing campaigns and leads are not duplicated.
    """
    fixtures_path = Path(fixtures_dir)

    imported_campaigns = 0
    imported_leads = 0
    skipped_leads = 0

    try:
        for campaign_id, campaign_config in config.campaigns.items():

            # Create the campaign if it doesn't exist.
            campaign = session.get(Campaign, campaign_id)

            if campaign is None:
                campaign = Campaign(
                    id=campaign_id,
                    tenant_id=campaign_config.tenant_id,
                    start_time=campaign_config.window_start,
                    end_time=campaign_config.window_end,
                    active_days=",".join(campaign_config.days),
                    max_attempts=campaign_config.max_attempts,
                    enabled=True,
                )
                session.add(campaign)
                imported_campaigns += 1

            session.flush()

            # Locate the configured CSV file.
            csv_path = fixtures_path / campaign_config.leads_file

            if not csv_path.is_file():
                raise FileNotFoundError(
                    f"Lead fixture not found: {csv_path}"
                )

            seen_lead_ids = set()

            with csv_path.open(
                "r",
                encoding="utf-8-sig",
                newline="",
            ) as file:
                reader = csv.DictReader(file)

                required_columns = {
                    "lead_id",
                    "phone",
                    "customer_name",
                }

                if not reader.fieldnames or not required_columns.issubset(
                    set(reader.fieldnames)
                ):
                    raise ValueError(
                        f"Invalid CSV columns: {csv_path.name}"
                    )

                for row in reader:
                    lead_id = (row.get("lead_id") or "").strip()
                    phone = (row.get("phone") or "").strip()
                    customer_name = (
                        row.get("customer_name") or ""
                    ).strip()

                    if not lead_id or not phone or not customer_name:
                        raise ValueError(
                            f"Missing lead data in {csv_path.name}"
                        )

                    if lead_id in seen_lead_ids:
                        raise ValueError(
                            f"Duplicate lead ID {lead_id} "
                            f"in {csv_path.name}"
                        )

                    seen_lead_ids.add(lead_id)

                    # Skip leads already imported for this campaign.
                    existing = session.get(
                        Lead,
                        (campaign_id, lead_id),
                    )

                    if existing is not None:
                        skipped_leads += 1
                        continue

                    normalized_phone = normalize_phone(phone)

                    lead = Lead(
                        campaign_id=campaign_id,
                        lead_id=lead_id,
                        phone_encrypted=encrypt_value(
                            normalized_phone
                        ),
                        phone_hash=hash_phone(normalized_phone),
                        customer_name_encrypted=encrypt_value(
                            customer_name
                        ),
                        attempts=0,
                        status="pending",
                    )

                    session.add(lead)
                    imported_leads += 1

        session.commit()

        return {
            "campaigns_imported": imported_campaigns,
            "leads_imported": imported_leads,
            "leads_skipped": skipped_leads,
        }

    except Exception:
        session.rollback()
        raise
