
import json
import logging
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from sqlalchemy.exc import SQLAlchemyError

from solution.app.config import load_config
from solution.app.database import SessionLocal, create_tables
from solution.app.security import verify_webhook_signature
from solution.app.webhooks import process_call_completed


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[2]

CONFIG_PATH = (
    PROJECT_ROOT
    / "assignment-b-workflow"
    / "fixtures"
    / "campaigns.json"
)

config = load_config(CONFIG_PATH)

create_tables()

app = FastAPI(
    title="Durable Campaign Dispatcher",
    version="1.0.0",
)


@app.get("/health")
def health():
    return {
        "status": "healthy",
        "service": "campaign-dispatcher",
    }


@app.post("/webhooks/provider")
async def provider_webhook(request: Request):
    # Read the original bytes because the provider signs the raw body.
    raw_body = await request.body()

    signature = request.headers.get("X-AU-Signature")

    if not verify_webhook_signature(
        raw_body,
        signature,
        config.webhook_secret,
    ):
        raise HTTPException(
            status_code=401,
            detail="Invalid webhook signature",
        )

    # Decode JSON only after verifying the signature.
    try:
        payload = json.loads(raw_body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise HTTPException(
            status_code=400,
            detail="Invalid JSON payload",
        )

    if not isinstance(payload, dict):
        raise HTTPException(
            status_code=400,
            detail="Webhook payload must be a JSON object",
        )

    session = SessionLocal()

    try:
        processed = process_call_completed(
            session,
            payload,
            config,
        )

        return {
            "status": "accepted",
            "processed": processed,
        }

    except ValueError as exc:
        logger.warning("Rejected webhook: %s", exc)

        raise HTTPException(
            status_code=400,
            detail=str(exc),
        )

    except SQLAlchemyError:
        session.rollback()
        logger.exception("Database error while processing webhook")

        raise HTTPException(
            status_code=500,
            detail="Webhook processing failed",
        )

    except Exception:
        session.rollback()
        logger.exception("Unexpected webhook processing error")

        raise HTTPException(
            status_code=500,
            detail="Internal webhook processing error",
        )

    finally:
        session.close()
