from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.auth import hash_password, verify_password
from app.main import create_app, database_url_from_env


def test_password_hash_is_salted_and_verifiable():
    first = hash_password("admin123@")
    second = hash_password("admin123@")
    assert first != second
    assert verify_password("admin123@", first)
    assert not verify_password("wrong", first)


def test_runtime_requires_postgresql_database_url(monkeypatch):
    monkeypatch.delenv("DFO_SPM_DATABASE_URL", raising=False)
    with pytest.raises(RuntimeError, match="DFO_SPM_DATABASE_URL"):
        database_url_from_env()
    monkeypatch.setenv("DFO_SPM_DATABASE_URL", "postgresql+psycopg://user:pass@db.example/dfo")
    assert database_url_from_env().startswith("postgresql+psycopg://")


def test_postgresql_bootstrap_requires_explicit_policy_compliant_password(monkeypatch):
    from app.main import bootstrap_admin_password

    monkeypatch.delenv("DFO_SPM_DEFAULT_ADMIN_PASSWORD", raising=False)
    with pytest.raises(RuntimeError, match="DEFAULT_ADMIN_PASSWORD"):
        bootstrap_admin_password("postgresql+psycopg://user:pass@db.example/dfo")
    monkeypatch.setenv("DFO_SPM_DEFAULT_ADMIN_PASSWORD", "weak")
    with pytest.raises(RuntimeError, match="12 characters"):
        bootstrap_admin_password("postgresql+psycopg://user:pass@db.example/dfo")
    monkeypatch.setenv("DFO_SPM_DEFAULT_ADMIN_PASSWORD", "StrongBootstrap!2026")
    assert bootstrap_admin_password("postgresql+psycopg://user:pass@db.example/dfo") == "StrongBootstrap!2026"


def test_default_admin_must_change_password_before_access(tmp_path: Path):
    app = create_app(f"sqlite:///{tmp_path / 'auth.db'}", seed=False)
    client = TestClient(app, follow_redirects=False)

    login = client.post("/login", data={"username": "admin", "password": "admin123@"})
    assert login.status_code == 303
    assert login.headers["location"] == "/change-password"
    assert client.get("/").headers["location"] == "/change-password"

    stale_session = TestClient(app, follow_redirects=False)
    assert stale_session.post("/login", data={"username": "admin", "password": "admin123@"}).status_code == 303

    changed = client.post("/change-password", data={
        "current_password": "admin123@",
        "new_password": "NewAdminPassword!2026",
        "confirm_password": "NewAdminPassword!2026",
    })
    assert changed.status_code == 303
    assert changed.headers["location"] == "/"
    assert client.get("/admin/users").status_code == 200
    assert stale_session.get("/").headers["location"] == "/login"


def test_admin_provisions_role_user_with_forced_password_change(tmp_path: Path):
    app = create_app(f"sqlite:///{tmp_path / 'users.db'}", seed=False)
    admin = TestClient(app, follow_redirects=False)
    admin.post("/login", data={"username": "admin", "password": "admin123@"})
    admin.post("/change-password", data={
        "current_password": "admin123@",
        "new_password": "NewAdminPassword!2026",
        "confirm_password": "NewAdminPassword!2026",
    })

    created = admin.post("/api/admin/users", json={
        "username": "evaluator.one",
        "display_name": "Evaluator One",
        "role": "EVALUATOR",
        "temporary_password": "TemporaryPassword!2026",
    })
    assert created.status_code == 201
    assert created.json()["must_change_password"] is True
    assert created.json()["role"] == "EVALUATOR"

    evaluator = TestClient(app, follow_redirects=False)
    login = evaluator.post("/login", data={"username": "evaluator.one", "password": "TemporaryPassword!2026"})
    assert login.headers["location"] == "/change-password"
    assert evaluator.get("/api/dashboard").status_code == 403
    evaluator.post("/change-password", data={
        "current_password": "TemporaryPassword!2026", "new_password": "EvaluatorPassword!2026", "confirm_password": "EvaluatorPassword!2026"
    })
    assert evaluator.get("/api/dashboard").status_code == 200
    assert admin.patch(f"/api/admin/users/{created.json()['id']}", json={"active": False}).status_code == 200
    assert evaluator.get("/api/dashboard").status_code == 401


def test_non_admin_cannot_provision_users(tmp_path: Path):
    app = create_app(f"sqlite:///{tmp_path / 'rbac.db'}", seed=False)
    admin = TestClient(app, follow_redirects=False)
    admin.post("/login", data={"username": "admin", "password": "admin123@"})
    admin.post("/change-password", data={
        "current_password": "admin123@", "new_password": "NewAdminPassword!2026", "confirm_password": "NewAdminPassword!2026"
    })
    admin.post("/api/admin/users", json={
        "username": "reviewer.one", "display_name": "Reviewer One", "role": "REVIEWER", "temporary_password": "TemporaryPassword!2026"
    })
    reviewer = TestClient(app, follow_redirects=False)
    reviewer.post("/login", data={"username": "reviewer.one", "password": "TemporaryPassword!2026"})
    reviewer.post("/change-password", data={
        "current_password": "TemporaryPassword!2026", "new_password": "ReviewerPassword!2026", "confirm_password": "ReviewerPassword!2026"
    })
    assert reviewer.post("/api/admin/users", json={
        "username": "approver.one", "display_name": "Approver One", "role": "APPROVER", "temporary_password": "TemporaryPassword!2026"
    }).status_code == 403
