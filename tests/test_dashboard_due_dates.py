from datetime import date, timedelta

from fastapi.testclient import TestClient

from app.domain import CONSTRUCTION_CRITERIA
from app.main import create_app
from app.models import Evaluation


def test_overview_counts_optional_due_dates_without_crashing(tmp_path):
    app = create_app(f"sqlite:///{tmp_path / 'dashboard.db'}", seed=False, auth_tokens={
        "fixture-admin": {"name": "Dashboard Test", "roles": ["ADMIN", "EVALUATOR"]},
    })
    client = TestClient(app, headers={"Authorization": "Bearer fixture-admin"})
    supplier = client.post("/api/suppliers", json={"name": "Dashboard Supplier"}).json()
    contract = client.post("/api/contracts", json={
        "supplier_id": supplier["id"], "contract_number": "DASHBOARD-001",
        "procurement_type": "Construction", "region": "Atlantic",
    }).json()
    today = date.today()
    cases = [(None, "DRAFT"), (-1, "DRAFT"), (0, "DRAFT"),
             (30, "DRAFT"), (31, "DRAFT"), (1, "APPROVED")]
    for offset, status in cases:
        # Evaluation date predates all due dates; status changes are test fixtures only.
        response = client.post("/api/evaluations", json={
            "contract_id": contract["id"], "model": "CONSTRUCTION",
            "evaluator": "Dashboard Test", "evaluation_date": (today - timedelta(days=2)).isoformat(),
            "due_date": (today + timedelta(days=offset)).isoformat() if offset is not None else None,
            "scores": {criterion: 15 for criterion in CONSTRUCTION_CRITERIA},
        })
        assert response.status_code == 201, response.text
        if status == "APPROVED":
            with app.state.session_factory() as db:
                db.get(Evaluation, response.json()["id"]).status = status
                db.commit()
    response = client.get("/api/dashboard")
    assert response.status_code == 200, response.text
    assert response.json()["evaluations_due"] == 2
    page = client.get("/")
    assert page.status_code == 200, page.text
    assert "Internal Server Error" not in page.text
