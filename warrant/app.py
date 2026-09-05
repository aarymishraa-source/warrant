"""
Warrant HTTP surface.

POST /webhooks/razorpay  -- verify, dedup, persist. No decision logic.
GET  /dashboard          -- experiment metrics, ITT, Newcombe CIs.
"""
from __future__ import annotations

import os
from pathlib import Path

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
from fastapi.responses import HTMLResponse
from jinja2 import Environment, FileSystemLoader

from warrant import config, db
from warrant.dashboard import build_dashboard
from warrant.ingest import (
    MissingEventIdError,
    SignatureError,
    ingest_event,
)
from warrant.ledger import stats as get_intent_summary

# Templates (pure server-rendered HTML, no JS framework)
_TEMPLATES = Environment(
    loader=FileSystemLoader(str(Path(__file__).parent.parent / "templates"))
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


app = FastAPI(title="WARRANT", docs_url=None, redoc_url=None, lifespan=lifespan)


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


@app.get("/", response_class=HTMLResponse)
async def landing(request: Request):
    """Landing page — experiment overview, no metrics."""
    conn = db.connect()
    try:
        db.init_db(conn)
        summary = get_intent_summary(conn) or {}
        total = summary.get("total", 0)
        lanes = [
            {"label": "SIM Lane Active" if total > 0 else "SIM Lane Idle",
             "css_class": "sim" if total > 0 else "offline"},
            {"label": "REAL Lane Idle", "css_class": "offline"},
        ]
        return HTMLResponse(_TEMPLATES.get_template("landing.html").render(
            request=request,
            cfg={
                "ASSIGNMENT_SALT": config.ASSIGNMENT_SALT,
                "ARM_WEIGHTS": dict(config.ARM_WEIGHTS),
                "LLM_CONFIDENCE_FLOOR": config.LLM_CONFIDENCE_FLOOR,
                "MAX_ATTEMPTS_PER_CASE": config.MAX_ATTEMPTS_PER_CASE,
                "NPCI_AUTOPAY_MAX_ATTEMPTS": config.NPCI_AUTOPAY_MAX_ATTEMPTS,
                "PREREGISTERED_SAMPLE_SIZE": config.PREREGISTERED_SAMPLE_SIZE,
                "SEED": str(config.SEED),
            },
            lanes=lanes,
            ledger_data={"total": total, "entries": []},
        ))
    finally:
        conn.close()


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request):
    """Experiment dashboard: ITT metrics, arm summaries, Newcombe confidence intervals."""
    conn = db.connect()
    try:
        db.init_db(conn)
        ctx = build_dashboard(conn)
        ctx["request"] = request
        # Pass only hashable values; avoid passing module objects through Jinja2
        ctx["cfg"] = {
            "ASSIGNMENT_SALT": config.ASSIGNMENT_SALT,
            "ARM_WEIGHTS": dict(config.ARM_WEIGHTS),
            "LLM_CONFIDENCE_FLOOR": config.LLM_CONFIDENCE_FLOOR,
            "MAX_ATTEMPTS_PER_CASE": config.MAX_ATTEMPTS_PER_CASE,
            "NPCI_AUTOPAY_MAX_ATTEMPTS": config.NPCI_AUTOPAY_MAX_ATTEMPTS,
            "PREREGISTERED_SAMPLE_SIZE": config.PREREGISTERED_SAMPLE_SIZE,
            "SEED": str(config.SEED),
        }
        return HTMLResponse(_TEMPLATES.get_template("dashboard.html").render(**ctx))
    except Exception as exc:
        ctx = {
            "request": request,
            "cfg": {
                "ASSIGNMENT_SALT": config.ASSIGNMENT_SALT,
                "ARM_WEIGHTS": dict(config.ARM_WEIGHTS),
                "LLM_CONFIDENCE_FLOOR": config.LLM_CONFIDENCE_FLOOR,
                "MAX_ATTEMPTS_PER_CASE": config.MAX_ATTEMPTS_PER_CASE,
                "NPCI_AUTOPAY_MAX_ATTEMPTS": config.NPCI_AUTOPAY_MAX_ATTEMPTS,
                "PREREGISTERED_SAMPLE_SIZE": config.PREREGISTERED_SAMPLE_SIZE,
                "SEED": str(config.SEED),
            },
            "error": str(exc),
            "lanes": [],
            "arm_data": None,
            "comparison": None,
        }
        return HTMLResponse(_TEMPLATES.get_template("dashboard.html").render(**ctx))
    finally:
        conn.close()
