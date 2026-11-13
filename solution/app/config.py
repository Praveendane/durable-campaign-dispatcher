
import json
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo


@dataclass(frozen=True)
class TenantConfig:
    tenant_id: str
    max_concurrent_calls: int


@dataclass(frozen=True)
class CampaignConfig:
    campaign_id: str
    tenant_id: str
    leads_file: str
    window_start: str
    window_end: str
    days: tuple[str, ...]
    max_attempts: int


@dataclass(frozen=True)
class RetryPolicy:
    no_answer_minutes: int
    busy_minutes: int
    failed_minutes: int


@dataclass(frozen=True)
class AppConfig:
    timezone: ZoneInfo
    sim_start: str
    regulatory_start: str
    regulatory_end: str
    window_grace_seconds: int
    webhook_secret: str
    retry_policy: RetryPolicy
    tenants: dict[str, TenantConfig]
    campaigns: dict[str, CampaignConfig]


def validate_time(value: str, field_name: str) -> None:
    try:
        from datetime import time
        time.fromisoformat(value)
        if len(value) != 5:
            raise ValueError
    except ValueError:
        raise ValueError(
            f"Invalid time for {field_name}: {value}"
        )


def load_config(config_path: str | Path) -> AppConfig:
    path = Path(config_path)

    with path.open("r", encoding="utf-8") as file:
        raw = json.load(file)

    timezone = ZoneInfo(raw["timezone"])

    tenants = {}
    for item in raw["tenants"]:
        tenant_id = item["tenant_id"]
        limit = item["max_concurrent_calls"]

        if tenant_id in tenants:
            raise ValueError(f"Duplicate tenant: {tenant_id}")
        if limit < 1:
            raise ValueError(f"Invalid concurrency limit: {tenant_id}")

        tenants[tenant_id] = TenantConfig(
            tenant_id=tenant_id,
            max_concurrent_calls=limit,
        )

    campaigns = {}
    for item in raw["campaigns"]:
        campaign_id = item["campaign_id"]
        tenant_id = item["tenant_id"]
        window = item["window"]

        if campaign_id in campaigns:
            raise ValueError(f"Duplicate campaign: {campaign_id}")
        if tenant_id not in tenants:
            raise ValueError(f"Unknown tenant: {tenant_id}")

        validate_time(window["start"], "campaign start")
        validate_time(window["end"], "campaign end")

        if window["start"] >= window["end"]:
            raise ValueError(f"Invalid calling window: {campaign_id}")

        if not window["days"]:
            raise ValueError(f"No calling days: {campaign_id}")

        if item["max_attempts"] < 1:
            raise ValueError(f"Invalid max attempts: {campaign_id}")

        campaigns[campaign_id] = CampaignConfig(
            campaign_id=campaign_id,
            tenant_id=tenant_id,
            leads_file=item["leads_file"],
            window_start=window["start"],
            window_end=window["end"],
            days=tuple(window["days"]),
            max_attempts=item["max_attempts"],
        )

    regulatory = raw["regulatory_window"]
    validate_time(regulatory["start"], "regulatory start")
    validate_time(regulatory["end"], "regulatory end")

    retry = raw["retry_policy"]

    return AppConfig(
        timezone=timezone,
        sim_start=raw["sim_start"],
        regulatory_start=regulatory["start"],
        regulatory_end=regulatory["end"],
        window_grace_seconds=raw["window_grace_seconds"],
        webhook_secret=raw["webhook_secret"],
        retry_policy=RetryPolicy(**retry),
        tenants=tenants,
        campaigns=campaigns,
    )
