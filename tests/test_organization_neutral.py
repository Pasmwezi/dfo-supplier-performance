from io import BytesIO
from pathlib import Path

import fitz
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.config import env
from app.domain import AE_DEFAULT_WEIGHTS, CONSTRUCTION_CRITERIA
from app.exports import correspondence_pdf
from app.main import create_app, database_url_from_env
from app.models import Contract


def client_for(tmp_path, seed=False):
    app = create_app(f"sqlite:///{tmp_path / 'neutral.db'}", seed=seed, auth_tokens={
        "qa": {"name": "Organization Evaluator", "roles": ["ADMIN", "EVALUATOR"]},
        "review": {"name": "Reviewer", "roles": ["REVIEWER"]},
        "approve": {"name": "Approver", "roles": ["APPROVER"]},
    })
    return TestClient(app, headers={"Authorization": "Bearer qa"})


def test_configuration_prefers_neutral_keys_and_preserves_legacy(monkeypatch):
    monkeypatch.setenv("DFO_SPM_DB_NAME", "installed_database")
    monkeypatch.delenv("SPM_DB_NAME", raising=False)
    assert env("SPM_DB_NAME") == "installed_database"
    monkeypatch.setenv("SPM_DB_NAME", "neutral_database")
    assert env("SPM_DB_NAME") == "neutral_database"
    assert env("DFO_SPM_DB_NAME") == "neutral_database"
    monkeypatch.setenv("SPM_DB_NAME", "")
    assert env("SPM_DB_NAME") == ""


def test_canonical_database_secret_configuration(tmp_path, monkeypatch):
    secret = tmp_path / "db-secret"
    secret.write_text("test-p@ssword")
    monkeypatch.delenv("SPM_DATABASE_URL", raising=False)
    monkeypatch.delenv("DFO_SPM_DATABASE_URL", raising=False)
    monkeypatch.setenv("SPM_DB_PASSWORD_FILE", str(secret))
    monkeypatch.setenv("SPM_DB_USER", "client")
    monkeypatch.setenv("SPM_DB_NAME", "client_spm")
    assert database_url_from_env() == "postgresql+psycopg://client:test-p%40ssword@postgres:5432/client_spm"


@pytest.mark.parametrize("organization", ["Transport Canada", "Example Private Corporation", "Fisheries and Oceans Canada"])
def test_correspondence_uses_the_record_organization(organization):
    content = correspondence_pdf({"organization": organization, "date": "2026-01-01", "supplier": "Example Supplier", "contract": "C-001", "subject": "Performance evaluation", "paragraphs": ["Recorded outcome."]})
    with fitz.open(stream=content, filetype="pdf") as document:
        text = "".join(page.get_text() for page in document)
    assert organization in text
    if organization != "Fisheries and Oceans Canada":
        assert "Fisheries and Oceans Canada" not in text


@pytest.mark.parametrize("regime", ["AE_EXTENDED", "DFO_AE_EXTENDED"])
def test_neutral_and_existing_extended_regimes_keep_same_scoring(tmp_path, regime):
    client = client_for(tmp_path)
    supplier = client.post("/api/suppliers", json={"name": "Private Sector Firm"}).json()
    response = client.post("/api/contracts", json={"supplier_id": supplier["id"], "contract_number": "PR-001", "procurement_type": "Engineering Services", "region": "Atlantic", "department": "Private Organization", "performance_regime": regime})
    assert response.status_code == 201, response.text
    contract = response.json()
    assert contract["department"] == "Private Organization"
    response = client.post("/api/evaluations", json={"contract_id": contract["id"], "model": "AE", "evaluator": "Organization Evaluator", "scores": {key: 16 for key in AE_DEFAULT_WEIGHTS}, "weights": AE_DEFAULT_WEIGHTS})
    assert response.status_code == 201, response.text
    assert response.json()["total_score"] == 80


def test_defaults_and_demo_are_organization_neutral(tmp_path):
    client = client_for(tmp_path, seed=True)
    assert client.app.title == "Supplier Performance Management System"
    contracts = client.get("/api/contracts").json()
    assert all(item["department"] == "Example Contracting Organization" for item in contracts)
    assert all(item["performance_regime"] != "DFO_AE_EXTENDED" for item in contracts)
    supplier = client.post("/api/suppliers", json={"name": "New Firm"}).json()
    response = client.post("/api/contracts", json={"supplier_id": supplier["id"], "contract_number": "NEW-001", "procurement_type": "Engineering Services", "region": "Atlantic"})
    assert response.json()["department"] == "Contracting Organization"
    assert response.json()["performance_regime"] == "AE_EXTENDED"


def test_rendered_pages_and_report_branding(tmp_path):
    client = client_for(tmp_path, seed=True)
    for route in ["/login", "/", "/suppliers", "/evaluations", "/evaluations/new", "/evaluations/1/view"]:
        response = client.get(route)
        assert response.status_code == 200, (route, response.text)
        assert "DFO" not in response.text
        assert "Fisheries and Oceans Canada" not in response.text
    form = client.get("/evaluations/new").text
    assert 'name="new_department"' in form
    assert "Organization Evaluator" in form
    report = client.get("/api/reports/performance-history.xlsx")
    assert report.status_code == 200
    assert 'filename="performance-history.xlsx"' in report.headers["content-disposition"]
    pdf = client.get("/api/reports/contractor-performance-history.pdf")
    with fitz.open(stream=pdf.content, filetype="pdf") as document:
        assert "DFO" not in "".join(page.get_text() for page in document)


def test_legacy_session_cookie_survives_upgrade_and_logout_revokes_it(tmp_path):
    client = TestClient(create_app(f"sqlite:///{tmp_path / 'sessions.db'}"), follow_redirects=False)
    response = client.post("/login", data={"username": "admin", "password": "admin123@"})
    assert response.status_code == 303
    assert "spm_session=" in response.headers["set-cookie"]
    token = client.cookies.get("spm_session")
    client.cookies.clear()
    client.cookies.set("dfo_spm_session", token)
    assert client.get("/change-password").status_code == 200
    response = client.post("/logout")
    assert response.status_code == 303
    assert any("spm_session=" in value for value in response.headers.get_list("set-cookie"))
    assert any("dfo_spm_session=" in value for value in response.headers.get_list("set-cookie"))
    client.cookies.set("dfo_spm_session", token)
    assert client.get("/change-password").status_code == 303


def test_logout_revokes_both_distinct_upgrade_cookie_sessions(tmp_path):
    app = create_app(f"sqlite:///{tmp_path / 'dual-sessions.db'}")
    canonical = TestClient(app, follow_redirects=False)
    legacy = TestClient(app, follow_redirects=False)
    for client in [canonical, legacy]:
        assert client.post("/login", data={"username": "admin", "password": "admin123@"}).status_code == 303
    canonical_token = canonical.cookies.get("spm_session")
    legacy_token = legacy.cookies.get("spm_session")
    assert canonical_token and legacy_token and canonical_token != legacy_token
    canonical.cookies.set("dfo_spm_session", legacy_token)
    assert canonical.post("/logout").status_code == 303
    for cookie, token in [("spm_session", canonical_token), ("dfo_spm_session", legacy_token)]:
        replay = TestClient(app, follow_redirects=False)
        replay.cookies.set(cookie, token)
        assert replay.get("/change-password").status_code == 303


def test_historical_records_are_not_rewritten(tmp_path):
    client = client_for(tmp_path)
    supplier = client.post("/api/suppliers", json={"name": "Historical Supplier"}).json()
    contract = client.post("/api/contracts", json={"supplier_id": supplier["id"], "contract_number": "HISTORY-001", "procurement_type": "Engineering Services", "region": "Atlantic", "department": "Fisheries and Oceans Canada", "performance_regime": "DFO_AE_EXTENDED"}).json()
    with client.app.state.session_factory() as db:
        stored = db.get(Contract, contract["id"])
        assert stored.department == "Fisheries and Oceans Canada"
        assert stored.performance_regime == "DFO_AE_EXTENDED"
