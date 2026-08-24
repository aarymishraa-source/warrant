"""
Warrant HTTP surface. One endpoint today.

POST /webhooks/razorpay  -- verify, dedup, persist. No decision logic.
"""
from __future__ import annotations

import os

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response

from warrant import db
from warrant.ingest import (
    IngestResult,
    MissingEventIdError,
    SignatureError,
    ingest_event,
)

def _secret() -> str:
    secret = os.environ.get("RAZORPAY_WEBHOOK_SECRET")
    if not secret:
        raise RuntimeError("RAZORPAY_WEBHOOK_SECRET is not set")
    return secret


@asynccontextmanager
async def lifespan(_: FastAPI):
    conn = db.connect()
    db.init_db(conn)
    conn.close()
    yield


app = FastAPI(title="Warrant", docs_url=None, redoc_url=None, lifespan=lifespan)


@app.post("/webhooks/razorpay")
async def razorpay_webhook(request: Request) -> Response:
    # RAW bytes. Not request.json(). See warrant/ingest.py.
    raw = await request.body()

    conn = db.connect()
    try:
        db.init_db(conn)
        outcome = ingest_event(conn, raw, dict(request.headers), _secret())
    except SignatureError:
        # 400 so the sender knows it was rejected; body is never processed.
        return Response(status_code=400, content='{"error":"invalid_signature"}',
                        media_type="application/json")
    except MissingEventIdError:
        return Response(status_code=400, content='{"error":"missing_event_id"}',
                        media_type="application/json")
    finally:
        conn.close()

    # 200 for duplicates too. A non-2xx would make the sender retry a delivery
    # we have already handled correctly.
    body = f'{{"result":"{outcome.result.value}","event_id":"{outcome.event_id}"}}'
    return Response(status_code=200, content=body, media_type="application/json")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
