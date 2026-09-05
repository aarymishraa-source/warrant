"""
Dashboard data layer.

Queries the append-only ledger to produce ITT metrics, arm summaries, and
Newcombe confidence intervals for the experiment dashboard.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

from warrant import config
from warrant.stats import DiffResult, newcombe_diff_ci

# ------------------------------------------------------------------------- data shapes

@dataclass
class ArmSummary:
    name: str
    tag_label: str
    tag_class: str
    n: int
    recovered: int
    rate_str: str
    avg_ticket_str: str
    carved_out: int
    n_executed: int
    n_failed: int
    n_unknown: int
    n_pending: int
    ci_label: str
    ci_left_pct: float
    ci_width_pct: float
    ci_css_class: str
    ci_lower_str: str
    ci_point_str: str
    ci_upper_str: str
    ci_excludes_class: str
    ci_excludes_str: str


@dataclass
class ComparisonRow:
    label: str
    diff_str: str
    diff_class: str
    ci_lower_str: str
    ci_upper_str: str
    excludes: bool
    verdict: str  # "positive" | "negative" | "inconclusive" | "insufficient"
    # CSS bar positions (0-100 scale, zero at 50)
    css_left_pct: float = 0.0
    css_width_pct: float = 0.0
    css_point_pct: float = 50.0
    css_class: str = "neutral"


@dataclass
class LaneInfo:
    label: str
    css_class: str


@dataclass
class FunnelStage:
    label: str
    count: int
    pct: float
    css_class: str


@dataclass
class FunnelData:
    stages: list[FunnelStage] = field(default_factory=list)
    total: int = 0


@dataclass
class PolicyRow:
    label: str
    count: int
    pct: float
    css_class: str


@dataclass
class PolicyData:
    rows: list[PolicyRow] = field(default_factory=list)


@dataclass
class RecentDecision:
    case_id: str
    customer_id: str
    arm: str
    arm_class: str
    carved_out: bool
    action_type: str
    status: str
    status_class: str
    created_at: str
    provider_ref: str


@dataclass
class IntentEntry:
    intent_id: str
    case_id: str
    action_type: str
    status: str
    external_ref: str | None
    attempt: int
    created_at: str


@dataclass
class LedgerData:
    entries: list[IntentEntry] = field(default_factory=list)
    total: int = 0


# ------------------------------------------------------------------------- auto-seed

def _seed_from_simulator(conn: sqlite3.Connection, n: int = 100) -> None:
    """Generate and persist n synthetic cases when the dashboard DB is empty.

    Uses the deterministic Simulator so the same seed always produces the same
    data — no flaky tests, reproducible demo data on every server start.
    """
    try:
        from warrant.sim import Simulator
    except Exception:
        return

    sim = Simulator(seed=0)
    now = _now()
    case_rows = []
    assignments_by_case = {}   # case_id -> (arm, carved_out, customer_id)
    intents = []

    for case in sim.generate_batch(n):
        # ── case state from ground truth ─────────────────────────────
        if case.outcome_treated is None:
            sim.simulate_treatment(case)

        if case.outcome_treated:
            state = "resolved_by_action"
        elif case.outcome_untouched:
            state = "resolved_externally"
        else:
            state = "exhausted"

        case_rows.append((
            case.case_id,
            case.customer_id,
            case.case_type,
            state,
            case.ticket_amount_paise,
            now,
        ))

        # ── assignment ──────────────────────────────────────────────
        from warrant.assign import assign_experiment, check_carveout
        arm_result = assign_experiment(case.customer_id)
        arm = arm_result["arm"]

        carved_out = 0
        if arm in ("RULES", "WARRANT"):
            carve_reason = check_carveout({"customer_id": case.customer_id})
            if carve_reason:
                arm = "CARVE_OUT"
                carved_out = 1

        assignments_by_case[case.case_id] = (arm, carved_out, case.customer_id)

        # ── intent ledger (RULES/WARRANT arms only) ─────────────────
        if arm in ("RULES", "WARRANT") and state == "resolved_by_action":
            import hashlib
            import uuid
            intent_id = uuid.uuid4().hex[:16]
            idem_key = hashlib.sha1(f"{case.case_id}-1".encode()).hexdigest()[:16]
            provider_ref = f"pl_test_{intent_id}"
            intents.append((
                intent_id,
                case.case_id,
                idem_key,
                "CREATE_LINK",
                0,
                "EXECUTED",
                provider_ref,
                now,
                now,
            ))

    # ── persist (INSERT OR REPLACE so re-seeding always overwrites) ──
    conn.execute("DELETE FROM cases")
    conn.execute("DELETE FROM assignments")
    conn.execute("DELETE FROM intents")
    conn.execute("DELETE FROM events")

    conn.executemany(
        """INSERT OR REPLACE INTO cases
           (case_id, customer_id, case_type, state, ticket_amount_paise, created_at)
           VALUES (?,?,?,?,?,?)""",
        case_rows,
    )

    for case_id, (arm, carved_out, customer_id) in assignments_by_case.items():
        conn.execute(
            """INSERT OR REPLACE INTO assignments
               (case_id, customer_id, arm, carved_out, assigned_at)
               VALUES (?,?,?,?,?)""",
            (case_id, customer_id, arm, carved_out, now),
        )

    if intents:
        conn.executemany(
            """INSERT OR REPLACE INTO intents
               (intent_id, case_id, idempotency_key, action_type, action_cost_paise,
                status, provider_ref, created_at, resolved_at)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            intents,
        )

    conn.commit()


# ------------------------------------------------------------------------- queries

def _now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")


def _all_arm_rows(conn: sqlite3.Connection) -> list[dict]:
    """Return one row per arm."""
    rows = conn.execute("""
        SELECT
            a.arm,
            COUNT(*) AS n,
            COALESCE(SUM(CASE
                WHEN c.state IN ('resolved_by_action', 'resolved_externally')
                THEN 1 ELSE 0 END), 0) AS recovered,
            COALESCE(SUM(CASE WHEN a.carved_out = 1 THEN 1 ELSE 0 END), 0) AS carved_out,
            COALESCE(SUM(c.ticket_amount_paise), 0) AS total_ticket,
            COALESCE(SUM(i.status = 'EXECUTED'), 0) AS n_executed,
            COALESCE(SUM(i.status = 'FAILED'), 0) AS n_failed,
            COALESCE(SUM(i.status = 'UNKNOWN'), 0) AS n_unknown,
            COALESCE(SUM(i.status = 'PENDING'), 0) AS n_pending
        FROM assignments a
        JOIN cases c ON c.case_id = a.case_id
        LEFT JOIN intents i ON i.case_id = a.case_id
        GROUP BY a.arm
    """).fetchall()
    return [dict(r) for r in rows]


# ------------------------------------------------------------------------- CI helpers
# CSS percentage properties are on DiffResult (warrant/stats.py), not here.

def _build_arm_summary(arm: str, counts: dict, diff: DiffResult | None) -> ArmSummary:
    n = counts["n"]
    recovered = counts["recovered"]
    carved_out = counts["carved_out"]
    total_ticket = counts["total_ticket"]
    n_executed = counts.get("n_executed", 0)
    n_failed = counts.get("n_failed", 0)
    n_unknown = counts.get("n_unknown", 0)
    n_pending = counts.get("n_pending", 0)
    rate = (recovered / n) if n > 0 else 0.0
    avg_ticket = (total_ticket // n) if n > 0 else 0

    tag_map = {
        "HOLDOUT":   ("Holdout", "tag-holdout"),
        "RULES":     ("Baseline", "tag-baseline"),
        "WARRANT":   ("Treatment", "tag-treatment"),
        "CARVE_OUT": ("Carved Out", "tag-holdout"),
    }
    tag_label, tag_class = tag_map.get(arm, (arm, "tag-holdout"))

    ci_label = "vs HOLDOUT"
    ci_left_pct = 50.0
    ci_width_pct = 0.0
    ci_css = "neutral"
    ci_lower_str = "—"
    ci_point_str = "—"
    ci_upper_str = "—"
    ci_excludes_class = "no"
    ci_excludes_str = "No comparison data"

    if diff is not None:
        ci_left_pct = diff.css_left_pct
        ci_width_pct = diff.css_width_pct
        ci_css = diff.css_class
        ci_lower_str = f"{diff.lower:+.1%}"
        ci_point_str = f"{diff.difference:+.1%}"
        ci_upper_str = f"{diff.upper:+.1%}"
        ci_excludes_class = "yes" if diff.excludes_zero else "no"
        ci_excludes_str = ("Excludes zero: effect is significant"
                           if diff.excludes_zero
                           else "Includes zero: effect not established")

    return ArmSummary(
        name=arm, tag_label=tag_label, tag_class=tag_class,
        n=n, recovered=recovered, rate_str=f"{rate:.1%}",
        avg_ticket_str=f"{avg_ticket:,}", carved_out=carved_out,
        n_executed=n_executed, n_failed=n_failed,
        n_unknown=n_unknown, n_pending=n_pending,
        ci_label=ci_label, ci_left_pct=ci_left_pct, ci_width_pct=ci_width_pct,
        ci_css_class=ci_css, ci_lower_str=ci_lower_str,
        ci_point_str=ci_point_str, ci_upper_str=ci_upper_str,
        ci_excludes_class=ci_excludes_class, ci_excludes_str=ci_excludes_str,
    )


def _build_comparison(arm_rows: list[dict]) -> list[ComparisonRow]:
    by_arm = {r["arm"]: r for r in arm_rows}

    def _row(label: str, treat_arm: str, ctrl_arm: str) -> ComparisonRow:
        t = by_arm.get(treat_arm)
        c = by_arm.get(ctrl_arm)
        if t is None or c is None or t["n"] < 2 or c["n"] < 2:
            return ComparisonRow(
                label=label, diff_str="—", diff_class="stat-maybe",
                ci_lower_str="—", ci_upper_str="—", excludes=False,
                verdict="insufficient",
            )
        n_t, n_c = t["n"], c["n"]
        s_t, s_c = t["recovered"], c["recovered"]
        try:
            diff = newcombe_diff_ci(s_t, n_t, s_c, n_c, alpha=0.10)
        except (ValueError, ZeroDivisionError):
            return ComparisonRow(
                label=label, diff_str="—", diff_class="stat-maybe",
                ci_lower_str="—", ci_upper_str="—", excludes=False,
                verdict="insufficient",
            )
        diff_str = f"{diff.difference:+.1%}"
        diff_class = ("stat-yes" if diff.difference > 0
                      else "stat-no" if diff.difference < 0
                      else "stat-maybe")
        verdict = "positive" if (diff.excludes_zero and diff.difference > 0) \
                  else "negative" if (diff.excludes_zero and diff.difference < 0) \
                  else "inconclusive"
        return ComparisonRow(
            label=label, diff_str=diff_str, diff_class=diff_class,
            ci_lower_str=f"{diff.lower:+.1%}", ci_upper_str=f"{diff.upper:+.1%}",
            excludes=diff.excludes_zero, verdict=verdict,
            css_left_pct=diff.css_left_pct,
            css_width_pct=diff.css_width_pct,
            css_point_pct=diff.css_point_pct,
            css_class=diff.css_class,
        )

    rows = []
    for label, treat, ctrl in [
        ("WARRANT vs HOLDOUT", "WARRANT", "HOLDOUT"),
        ("RULES vs HOLDOUT",   "RULES",   "HOLDOUT"),
    ]:
        r = _row(label, treat, ctrl)
        if r.verdict != "insufficient":
            rows.append(r)
    return rows


def _detect_lanes(conn: sqlite3.Connection) -> list[LaneInfo]:
    rows = conn.execute(
        "SELECT event_type FROM events GROUP BY event_type"
    ).fetchall()
    event_types = {r["event_type"] for r in rows}
    case_count = conn.execute("SELECT COUNT(*) AS n FROM cases").fetchone()["n"]
    return [
        LaneInfo(
            label="SIM Lane Active" if case_count > 0 else "SIM Lane Idle",
            css_class="sim" if case_count > 0 else "offline",
        ),
        LaneInfo(
            label="REAL Lane Active" if event_types else "REAL Lane Idle",
            css_class="real" if event_types else "offline",
        ),
    ]


def _fetch_ledger_entries(conn: sqlite3.Connection, limit: int = 50) -> LedgerData:
    total_row = conn.execute("SELECT COUNT(*) AS cnt FROM intents").fetchone()
    total = dict(total_row)["cnt"] if total_row else 0
    rows = conn.execute("""
        SELECT intent_id, case_id, action_type, status, provider_ref, created_at
        FROM intents ORDER BY created_at DESC LIMIT ?
    """, (limit,)).fetchall()
    entries = [
        IntentEntry(
            intent_id=str(r["intent_id"]),
            case_id=str(r["case_id"]),
            action_type=str(r["action_type"]),
            status=str(r["status"]),
            external_ref=str(r["provider_ref"]) if r["provider_ref"] else None,
            attempt=1,
            created_at=str(r["created_at"]),
        )
        for r in rows
    ]
    return LedgerData(entries=entries, total=total)


# ------------------------------------------------------------------------- funnel & policy

def _fetch_funnel_data(conn: sqlite3.Connection) -> FunnelData:
    """Recovery funnel: assigned → intent created → executed."""
    # Total cases assigned to treatment arms
    total_assigned = conn.execute("""
        SELECT COUNT(*) AS n FROM assignments WHERE arm IN ('RULES', 'WARRANT')
    """).fetchone()["n"] or 0

    # Intents created from those assignments
    intents_created = conn.execute("""
        SELECT COUNT(DISTINCT i.case_id) AS n
        FROM intents i
        JOIN assignments a ON a.case_id = i.case_id
        WHERE a.arm IN ('RULES', 'WARRANT')
    """).fetchone()["n"] or 0

    # Intents executed
    n_executed = conn.execute("""
        SELECT COUNT(*) AS n FROM intents WHERE status = 'EXECUTED'
    """).fetchone()["n"] or 0

    # Intents with provider resolution (executed + failed)
    n_resolved = conn.execute("""
        SELECT COUNT(*) AS n
        FROM intents
        WHERE status IN ('EXECUTED', 'FAILED')
    """).fetchone()["n"] or 0

    # Recovered (resolved_by_action) across treatment arms
    n_recovered = conn.execute("""
        SELECT COUNT(*) AS n
        FROM cases c
        JOIN assignments a ON a.case_id = c.case_id
        WHERE a.arm IN ('RULES', 'WARRANT')
          AND c.state = 'resolved_by_action'
    """).fetchone()["n"] or 0

    def stage(label: str, count: int, css_class: str) -> FunnelStage:
        pct = (count / total_assigned * 100) if total_assigned > 0 else 0.0
        return FunnelStage(label=label, count=count, pct=pct, css_class=css_class)

    return FunnelData(
        stages=[
            stage("Assigned to Treatment", total_assigned, "assigned"),
            stage("Intent Proposed", intents_created, "proposed"),
            stage("Intent Executed", n_executed, "executed"),
            stage("Provider Resolved", n_resolved, "resolved"),
            stage("Recovered After Action", n_recovered, "recovered"),
        ],
        total=total_assigned,
    )


def _fetch_policy_data(conn: sqlite3.Connection) -> PolicyData:
    """Policy decision breakdown from intent status."""
    rows = conn.execute("""
        SELECT
            CASE
                WHEN status = 'EXECUTED' THEN 'Execute'
                WHEN status = 'FAILED'   THEN 'Refused'
                WHEN status = 'UNKNOWN'  THEN 'Unknown'
                WHEN status = 'PENDING'  THEN 'Pending'
                ELSE status
            END AS label,
            COUNT(*) AS count
        FROM intents
        GROUP BY label
        ORDER BY count DESC
    """).fetchall()

    total = sum(r["count"] for r in rows) or 1  # avoid div/0

    status_class_map = {
        "Execute":  "execute",
        "Refused":  "refused",
        "Unknown":  "unknown",
        "Pending":  "pending",
    }

    policy_rows = []
    for r in rows:
        label = r["label"]
        count = r["count"]
        pct = count / total * 100
        css_class = status_class_map.get(label, "unknown")
        policy_rows.append(PolicyRow(
            label=label,
            count=count,
            pct=pct,
            css_class=css_class,
        ))

    # Pad with placeholder rows if some outcomes are missing
    seen_labels = {pr.label for pr in policy_rows}
    for label, css_class in status_class_map.items():
        if label not in seen_labels:
            policy_rows.append(PolicyRow(
                label=label, count=0, pct=0.0, css_class=css_class,
            ))

    return PolicyData(rows=policy_rows)


def _fetch_recent_decisions(conn: sqlite3.Connection, limit: int = 10) -> list[RecentDecision]:
    """Most recent intent decisions for the operational view."""
    rows = conn.execute("""
        SELECT
            i.case_id,
            a.customer_id,
            a.arm,
            a.carved_out,
            c.ticket_amount_paise,
            i.action_type,
            i.status,
            i.provider_ref,
            i.created_at
        FROM intents i
        JOIN assignments a ON a.case_id = i.case_id
        JOIN cases c ON c.case_id = i.case_id
        ORDER BY i.created_at DESC
        LIMIT ?
    """, (limit,)).fetchall()

    arm_class_map = {
        "HOLDOUT":   "holdout",
        "RULES":     "baseline",
        "WARRANT":   "treatment",
        "CARVE_OUT": "carved",
    }
    status_class_map = {
        "EXECUTED": "executed",
        "FAILED":   "refused",
        "UNKNOWN":  "unknown",
        "PENDING":  "pending",
    }

    return [
        RecentDecision(
            case_id=str(r["case_id"]),
            customer_id=str(r["customer_id"]),
            arm=str(r["arm"]),
            arm_class=arm_class_map.get(str(r["arm"]), "holdout"),
            carved_out=bool(r["carved_out"]),
            action_type=str(r["action_type"]),
            status=str(r["status"]),
            status_class=status_class_map.get(str(r["status"]), "unknown"),
            created_at=str(r["created_at"]),
            provider_ref=str(r["provider_ref"]) if r["provider_ref"] else "—",
        )
        for r in rows
    ]


# ------------------------------------------------------------------------- public API

def build_dashboard(conn: sqlite3.Connection) -> dict:
    """Compute all data needed for the dashboard template.

    Returns a dict with all metrics pre-computed as top-level keys so the
    template never has to aggregate from nested structures.
    """
    lanes = _detect_lanes(conn)
    arm_rows = _all_arm_rows(conn)
    ledger_data = _fetch_ledger_entries(conn)

    # Auto-seed on empty DB so dashboard always shows live data
    if not arm_rows:
        _seed_from_simulator(conn, n=100)
        arm_rows = _all_arm_rows(conn)
        ledger_data = _fetch_ledger_entries(conn)

    by_arm = {r["arm"]: r for r in arm_rows}

    # ── Pre-compute all metrics ─────────────────────────────────────
    total_assigned  = sum(r["n"] for r in arm_rows)
    total_recovered = sum(r["recovered"] for r in arm_rows)
    itt_rate        = round(total_recovered / total_assigned * 100, 1) if total_assigned > 0 else 0.0

    warrant_counts  = by_arm.get("WARRANT",  {"n": 0, "recovered": 0})
    holdout_counts  = by_arm.get("HOLDOUT",  {"n": 0, "recovered": 0})
    rules_counts    = by_arm.get("RULES",    {"n": 0, "recovered": 0})

    warrant_rate  = (warrant_counts["recovered"] / warrant_counts["n"] * 100) \
                    if warrant_counts["n"] > 0 else None
    holdout_rate  = (holdout_counts["recovered"] / holdout_counts["n"] * 100) \
                    if holdout_counts["n"] > 0 else None
    rules_rate    = (rules_counts["recovered"]   / rules_counts["n"] * 100) \
                    if rules_counts["n"] > 0 else None

    warrant_lift  = round(warrant_rate - holdout_rate, 2) \
                    if (warrant_rate is not None and holdout_rate is not None) else None
    rules_lift    = round(rules_rate   - holdout_rate, 2) \
                    if (rules_rate   is not None and holdout_rate is not None) else None

    active_intents = ledger_data.total

    # ── WARRANT vs HOLDOUT CI diff ──────────────────────────────────
    warrant_diff: DiffResult | None = None
    if warrant_counts["n"] >= 2 and holdout_counts["n"] >= 2:
        try:
            warrant_diff = newcombe_diff_ci(
                warrant_counts["recovered"], warrant_counts["n"],
                holdout_counts["recovered"], holdout_counts["n"],
                alpha=0.10,
            )
        except (ValueError, ZeroDivisionError):
            pass

    # ── Arm summaries ───────────────────────────────────────────────
    arm_order = ["HOLDOUT", "RULES", "WARRANT", "CARVE_OUT"]
    arm_data = []
    for arm in arm_order:
        counts = by_arm.get(arm, {
            "n": 0, "recovered": 0, "carved_out": 0, "total_ticket": 0,
            "n_executed": 0, "n_failed": 0, "n_unknown": 0, "n_pending": 0
        })
        if counts["n"] == 0 and arm != "CARVE_OUT":
            continue
        diff = warrant_diff if arm == "WARRANT" else None
        arm_data.append(_build_arm_summary(arm, counts, diff))

    # ── Comparison rows ─────────────────────────────────────────────
    comparison_rows = _build_comparison(arm_rows)

    # ── HOLDOUT progress ────────────────────────────────────────────
    holdout_progress = round(
        holdout_counts["n"] / config.PREREGISTERED_SAMPLE_SIZE * 100, 1
    ) if holdout_counts["n"] > 0 else 0.0

    # ── Funnel ─────────────────────────────────────────────────────
    funnel_data = _fetch_funnel_data(conn)

    # ── Policy decisions ───────────────────────────────────────────
    policy_data = _fetch_policy_data(conn)

    # ── Recent decisions ─────────────────────────────────────────────
    recent_decisions = _fetch_recent_decisions(conn)

    # ── Experiment status ──────────────────────────────────────────
    # Determine experiment phase label from HOLDOUT fill
    if holdout_progress >= 100:
        exp_status = "Enrollment Closed"
        exp_css = "closed"
    elif holdout_progress >= 50 or holdout_counts["n"] > 0:
        exp_status = "Enrollment Active"
        exp_css = "active"
    else:
        exp_status = "Simulating"
        exp_css = "sim"

    return dict(
        # Lanes
        lanes=lanes,
        error=None,
        # ── Pre-computed top-level metrics ────────────────────────────
        total_assigned=total_assigned,
        total_recovered=total_recovered,
        itt_rate=itt_rate,
        warrant_lift=warrant_lift,
        rules_lift=rules_lift,
        warrant_n=warrant_counts["n"],
        holdout_n=holdout_counts["n"],
        rules_n=rules_counts["n"],
        warrant_rate=(
            round(warrant_rate, 1) if warrant_rate is not None else None
        ),
        holdout_rate=(
            round(holdout_rate, 1) if holdout_rate is not None else None
        ),
        rules_rate=(
            round(rules_rate, 1) if rules_rate is not None else None
        ),
        active_intents=active_intents,
        holdout_progress=holdout_progress,
        exp_status=exp_status,
        exp_css=exp_css,
        # ── Structured data ──────────────────────────────────────────
        arm_data=arm_data,
        comparison_rows=comparison_rows,
        ledger_data=ledger_data,
        funnel_data=funnel_data,
        policy_data=policy_data,
        recent_decisions=recent_decisions,
    )
