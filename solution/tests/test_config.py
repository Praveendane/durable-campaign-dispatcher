
from pathlib import Path

from solution.app.config import load_config


ROOT = Path(__file__).resolve().parents[2]

CONFIG_PATH = (
    ROOT
    / "assignment-b-workflow"
    / "fixtures"
    / "campaigns.json"
)


def test_load_campaign_configuration():
    config = load_config(CONFIG_PATH)

    assert len(config.tenants) == 2
    assert len(config.campaigns) == 3

    assert (
        config.tenants["acme_insurance"].max_concurrent_calls
        == 4
    )

    assert (
        config.campaigns["c_acme_renewals"].max_attempts
        == 3
    )

    assert config.timezone.key == "Asia/Kolkata"
