
from pathlib import Path

from sqlalchemy import create_engine, select, func
from sqlalchemy.orm import sessionmaker

from solution.app.database import Base
from solution.app.config import load_config
from solution.app.importer import import_campaign_data
from solution.app.models import Campaign, Lead
from solution.app.security import decrypt_value


ROOT = Path(__file__).resolve().parents[2]

CONFIG_PATH = (
    ROOT / "assignment-b-workflow" / "fixtures" / "campaigns.json"
)

FIXTURES_DIR = ROOT / "assignment-b-workflow" / "fixtures"


def test_import_all_campaign_leads():
    test_engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(test_engine)

    TestSession = sessionmaker(bind=test_engine)
    config = load_config(CONFIG_PATH)

    with TestSession() as session:
        result = import_campaign_data(
            session,
            config,
            FIXTURES_DIR,
        )

        campaign_count = session.scalar(
            select(func.count()).select_from(Campaign)
        )

        lead_count = session.scalar(
            select(func.count()).select_from(Lead)
        )

        assert result["campaigns_imported"] == 3
        assert result["leads_imported"] == 200
        assert campaign_count == 3
        assert lead_count == 200

        first_lead = session.scalars(
            select(Lead).limit(1)
        ).first()

        assert first_lead is not None

        # Ensure customer details are encrypted in the database.
        assert " " not in first_lead.phone_encrypted
        assert decrypt_value(
            first_lead.phone_encrypted
        ).startswith("+")

        assert decrypt_value(
            first_lead.customer_name_encrypted
        )

    test_engine.dispose()


def test_import_is_idempotent():
    test_engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(test_engine)

    TestSession = sessionmaker(bind=test_engine)
    config = load_config(CONFIG_PATH)

    with TestSession() as session:
        first = import_campaign_data(
            session, config, FIXTURES_DIR
        )

        second = import_campaign_data(
            session, config, FIXTURES_DIR
        )

        assert first["leads_imported"] == 200
        assert second["leads_imported"] == 0
        assert second["leads_skipped"] == 200

    test_engine.dispose()
