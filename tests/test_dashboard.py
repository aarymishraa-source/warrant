"""Tests for the /dashboard HTTP endpoint."""

import pytest
from fastapi.testclient import TestClient

from warrant import config, db
from warrant.app import app


def _fresh_db():
    conn = db.connect(":memory:")
    db.init_db(conn)
    return conn


class TestDashboardEndpoint:
    def test_returns_200(self):
        """GET /dashboard returns 200 even on empty DB."""
        client = TestClient(app)
        resp = client.get("/dashboard")
        assert resp.status_code == 200

    def test_content_type_is_html(self):
        client = TestClient(app)
        resp = client.get("/dashboard")
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]

    def test_html_contains_warrant_title(self):
        client = TestClient(app)
        resp = client.get("/dashboard")
        assert resp.status_code == 200
        assert "WARRANT" in resp.text

    def test_html_contains_sim_lane_badge(self):
        client = TestClient(app)
        resp = client.get("/dashboard")
        # Template renders "SIM" lane indicator text and lane-dot CSS class
        assert "SIM" in resp.text.upper()
        assert "lane-dot" in resp.text

    def test_html_contains_allocation_section(self):
        client = TestClient(app)
        resp = client.get("/dashboard")
        assert "Experiment Allocation" in resp.text or "Target Enrollment" in resp.text

    def test_seeded_state_shows_arm_data(self):
        """Dashboard auto-seeds on empty DB, so arm data is always present."""
        client = TestClient(app)
        resp = client.get("/dashboard")
        assert resp.status_code == 200
        # Auto-seed populates HOLDOUT and RULES arms
        assert "Holdout" in resp.text
        assert "Baseline" in resp.text

    def test_arm_card_renders_with_data(self):
        """With cases in the DB, an arm card is present."""
        conn = _fresh_db()
        conn.execute(
            "INSERT INTO assignments (case_id, customer_id, arm, carved_out, assigned_at) VALUES (?, ?, ?, ?, ?)",
            ("case_001", "cust_001", "HOLDOUT", 0, "2026-08-24T00:00:00+00:00"),
        )
        conn.execute(
            """INSERT INTO cases
               (case_id, customer_id, case_type, ticket_amount_paise, state)
               VALUES (?, ?, ?, ?, ?)""",
            ("case_001", "cust_001", "one_time_link", 25000, "resolved_externally"),
        )
        conn.commit()
        conn.close()

        client = TestClient(app)
        resp = client.get("/dashboard")
        assert resp.status_code == 200
        assert "HOLDOUT" in resp.text

    def test_comparison_row_renders_when_both_arms_have_data(self):
        """When HOLDOUT and WARRANT each have ≥2 cases, comparison renders."""
        conn = _fresh_db()
        for i, arm in enumerate(["HOLDOUT"] * 3 + ["WARRANT"] * 3):
            conn.execute(
                "INSERT INTO assignments (case_id, customer_id, arm, carved_out, assigned_at) VALUES (?, ?, ?, ?, ?)",
                (f"case_{i:03d}", f"cust_{i:03d}", arm, 0, "2026-08-24T00:00:00+00:00"),
            )
            resolved = "resolved_by_action" if i % 2 == 0 else "resolved_externally"
            conn.execute(
                """INSERT INTO cases
                   (case_id, customer_id, case_type, ticket_amount_paise, state)
                   VALUES (?, ?, ?, ?, ?)""",
                (f"case_{i:03d}", f"cust_{i:03d}", "one_time_link",
                 25000, resolved),
            )
        conn.commit()
        conn.close()

        client = TestClient(app)
        resp = client.get("/dashboard")
        assert resp.status_code == 200
        assert "WARRANT" in resp.text

    def test_ci_bar_present_when_comparison_exists(self):
        """CI bar markup appears when comparison data is available."""
        conn = _fresh_db()
        for i, arm in enumerate(["HOLDOUT"] * 3 + ["WARRANT"] * 3):
            conn.execute(
                "INSERT INTO assignments (case_id, customer_id, arm, carved_out, assigned_at) VALUES (?, ?, ?, ?, ?)",
                (f"case_{i:03d}", f"cust_{i:03d}", arm, 0, "2026-08-24T00:00:00+00:00"),
            )
            conn.execute(
                """INSERT INTO cases
                   (case_id, customer_id, case_type, ticket_amount_paise, state)
                   VALUES (?, ?, ?, ?, ?)""",
                (f"case_{i:03d}", f"cust_{i:03d}", "one_time_link",
                 25000, "resolved_by_action"),
            )
        conn.commit()
        conn.close()

        client = TestClient(app)
        resp = client.get("/dashboard")
        assert "ci-track" in resp.text

    def test_arm_names_in_allocation_section(self):
        """Allocation section shows all three arms."""
        client = TestClient(app)
        resp = client.get("/dashboard")
        assert "HOLDOUT" in resp.text
        assert "RULES" in resp.text
        assert "WARRANT" in resp.text

    def test_dashboard_does_not_leak_stack_trace(self):
        """Errors are caught and the template renders the error state."""
        client = TestClient(app)
        resp = client.get("/dashboard")
        # No Python traceback in HTML
        assert "Traceback" not in resp.text
        assert "AttributeError" not in resp.text


class TestHealthEndpoint:
    def test_health_returns_200(self):
        client = TestClient(app)
        resp = client.get("/health")
        assert resp.status_code == 200

    def test_health_returns_ok(self):
        client = TestClient(app)
        resp = client.get("/health")
        data = resp.json()
        assert data["status"] == "ok"
