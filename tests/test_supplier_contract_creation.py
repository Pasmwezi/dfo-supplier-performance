"""Evaluation identification modes and transaction regressions."""
import json

import pytest

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.domain import CONSTRUCTION_CRITERIA
from app.main import create_app
from app.models import AuditEntry, Contract, Evaluation, EvaluationVersion, Supplier


def client_for(tmp_path):
    app = create_app(f"sqlite:///{tmp_path / 'contracts.db'}", seed=False, auth_tokens={
        "admin": {"name": "Administrator", "roles": ["ADMIN"]},
        "evaluator": {"name": "Evaluator", "roles": ["EVALUATOR"]},
        "reviewer": {"name": "Reviewer", "roles": ["REVIEWER"]},
    })
    return TestClient(app, headers={"Authorization": "Bearer evaluator"})


def payload(number="SECOND-1"):
    return {
        "new_contract": {"contract_number": number, "procurement_type": "Construction",
                         "region": "Atlantic", "performance_regime": "GI16_GC1_22"},
        "model": "CONSTRUCTION", "evaluator": "Evaluator",
        "scores": {name: 15 for name in CONSTRUCTION_CRITERIA},
    }


def test_evaluator_adds_two_distinct_contracts_to_one_supplier_with_audit(tmp_path):
    client = client_for(tmp_path)
    supplier = client.post("/api/suppliers", headers={"Authorization": "Bearer admin"},
                           json={"name": "Shared Supplier", "business_number": "BN-ONE"}).json()
    for number in ("FIRST-1", "SECOND-2"):
        response = client.post("/api/evaluations", json=payload(number) | {"supplier_id": supplier["id"]})
        assert response.status_code == 201, response.text
        ev = response.json()
        assert ev["status"] == "DRAFT"
        assert ev["total_score"] == 75
        history = client.get(f"/api/evaluations/{ev['id']}/history").json()
        assert len(history["versions"]) == 1
        audit = history["audit"]
        assert {entry["action"] for entry in audit} == {"CREATE_CONTRACT_WITH_EVALUATION", "CREATE"}
        entry = next(entry for entry in audit if entry["action"] == "CREATE_CONTRACT_WITH_EVALUATION")
        assert entry["user"].startswith("Evaluator [")
        assert json.loads(entry["revised_value"])["supplier_id"] == supplier["id"]
        assert json.loads(entry["revised_value"])["contract_number"] == number
    assert client.get("/api/suppliers").json() == [supplier]
    contracts = client.get("/api/contracts").json()
    assert len(contracts) == 2
    assert {contract["supplier_id"] for contract in contracts} == {supplier["id"]}
    assert len(client.get("/api/evaluations").json()) == 2


def counts(client):
    with client.app.state.session_factory() as db:
        return {model.__name__: db.scalar(select(func.count()).select_from(model))
                for model in (Supplier, Contract, Evaluation, EvaluationVersion, AuditEntry)}


def test_unknown_supplier_is_not_found_and_leaves_no_records(tmp_path):
    client = client_for(tmp_path)
    before = counts(client)
    response = client.post("/api/evaluations", json=payload() | {"supplier_id": 999999})
    assert response.status_code == 404, response.text
    assert response.json()["detail"] == "Supplier not found."
    assert counts(client) == before


@pytest.mark.parametrize("identifiers", [
    {"contract_id": 1, "supplier_id": 1},
    {"contract_id": 1, "new_supplier": {"name": "Unexpected Supplier"}},
    {"contract_id": 1, "new_contract": True},
    {"contract_id": 1, "supplier_id": 1, "new_contract": True},
    {"contract_id": 1, "new_supplier": {"name": "Unexpected Supplier"}, "new_contract": True},
    {"supplier_id": 1, "new_supplier": {"name": "Unexpected Supplier"}, "new_contract": True},
    {"supplier_id": 1}, {"new_supplier": {"name": "Incomplete Supplier"}},
    {"new_contract": True}, {},
])
def test_conflicting_or_incomplete_identification_is_rejected(tmp_path, identifiers):
    client = client_for(tmp_path)
    original = client.post("/api/evaluations", json=payload("ORIGINAL-1") | {
        "new_supplier": {"name": "Original Supplier"},
    })
    assert original.status_code == 201, original.text
    data = payload()
    contract = data.pop("new_contract")
    data.update(identifiers)
    if data.get("new_contract") is True:
        data["new_contract"] = contract
    before = counts(client)
    response = client.post("/api/evaluations", json=data)
    assert response.status_code == 422, response.text
    assert counts(client) == before


@pytest.mark.parametrize("new_supplier", [False, True])
def test_duplicate_contract_number_rolls_back_every_record(tmp_path, new_supplier):
    client = client_for(tmp_path)
    original = client.post("/api/evaluations", json=payload("UNIQUE-1") | {
        "new_supplier": {"name": "Original Supplier"},
    }).json()
    before = counts(client)
    identification = ({"new_supplier": {"name": "Must Not Be Created"}} if new_supplier
                      else {"supplier_id": client.get("/api/suppliers").json()[0]["id"]})
    response = client.post("/api/evaluations", json=payload("UNIQUE-1") | identification)
    assert response.status_code == 409, response.text
    assert counts(client) == before
    assert client.get(f"/api/evaluations/{original['id']}").json()["status"] == "DRAFT"
    # The session remains usable and the unique new number succeeds afterward.
    assert client.post("/api/evaluations", json=payload("UNIQUE-2") | identification).status_code == 201


def test_legacy_modes_keep_their_records_and_audit(tmp_path):
    client = client_for(tmp_path)
    response = client.post("/api/evaluations", json=payload("LEGACY-1") | {
        "new_supplier": {"name": "Legacy Supplier"},
    })
    assert response.status_code == 201, response.text
    original = response.json()
    actions = client.get(f"/api/evaluations/{original['id']}/history").json()["audit"]
    assert {entry["action"] for entry in actions} == {
        "CREATE_SUPPLIER_WITH_EVALUATION", "CREATE_CONTRACT_WITH_EVALUATION", "CREATE",
    }
    data = payload()
    data.pop("new_contract")
    response = client.post("/api/evaluations", json=data | {"contract_id": original["contract_id"]})
    assert response.status_code == 201, response.text
    actions = client.get(f"/api/evaluations/{response.json()['id']}/history").json()["audit"]
    assert [entry["action"] for entry in actions] == ["CREATE"]
    assert counts(client) == {"Supplier": 1, "Contract": 1, "Evaluation": 2,
                              "EvaluationVersion": 2, "AuditEntry": 4}


@pytest.mark.parametrize("invalid_contract", [
    {"contract_number": "X"}, {"contract_value": -1},
    {"start_date": "2026-05-02", "end_date": "2026-05-01"},
    {"performance_regime": "GI23_GC26_2913_1"},
])
def test_existing_supplier_new_contract_keeps_contract_validation(tmp_path, invalid_contract):
    client = client_for(tmp_path)
    supplier = client.post("/api/suppliers", headers={"Authorization": "Bearer admin"},
                           json={"name": "Validated Supplier"}).json()
    data = payload() | {"supplier_id": supplier["id"]}
    data["new_contract"].update(invalid_contract)
    before = counts(client)
    assert client.post("/api/evaluations", json=data).status_code == 422
    assert counts(client) == before


def test_new_contract_mode_retains_evaluator_role_control(tmp_path):
    client = client_for(tmp_path)
    response = client.post("/api/evaluations", json=payload() | {"supplier_id": 999},
                           headers={"Authorization": "Bearer reviewer"})
    assert response.status_code == 403
    assert counts(client)["Contract"] == 0


def test_form_offers_existing_supplier_and_shared_new_contract_fields(tmp_path):
    client = client_for(tmp_path)
    supplier = client.post("/api/suppliers", headers={"Authorization": "Bearer admin"},
                           json={"name": "Dropdown Supplier"}).json()
    page = client.get("/evaluations/new").text
    assert 'value="new_contract"' in page
    assert 'id="existing-supplier-fields"' in page
    assert 'name="supplier_id"' in page
    assert f'<option value="{supplier["id"]}">Dropdown Supplier</option>' in page
    assert 'id="new-contract-fields"' in page
    assert page.count('name="new_contract_number"') == 1
    assert 'value="existing" checked' in page
    assert 'value="new"' in page
