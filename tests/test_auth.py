from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.auth import hash_password, verify_password
from app.main import create_app, database_url_from_env, validate_runtime_settings
from app.models import AccessAudit


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


def test_runtime_builds_database_url_from_docker_secret(tmp_path, monkeypatch):
    secret = tmp_path / "database-password"
    secret.write_text("p@ss:/?#%\n")
    monkeypatch.delenv("DFO_SPM_DATABASE_URL", raising=False)
    monkeypatch.setenv("DFO_SPM_DB_PASSWORD_FILE", str(secret))
    monkeypatch.setenv("DFO_SPM_DB_USER", "dfo user")
    monkeypatch.setenv("DFO_SPM_DB_HOST", "postgres")
    monkeypatch.setenv("DFO_SPM_DB_PORT", "5432")
    monkeypatch.setenv("DFO_SPM_DB_NAME", "dfo-spm")

    assert database_url_from_env() == "postgresql+psycopg://dfo%20user:p%40ss%3A%2F%3F%23%25@postgres:5432/dfo-spm"


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


def test_postgresql_bootstrap_reads_password_from_docker_secret(tmp_path, monkeypatch):
    from app.main import bootstrap_admin_password

    secret = tmp_path / "admin-password"
    secret.write_text("StrongSecret!2026\n")
    monkeypatch.delenv("DFO_SPM_DEFAULT_ADMIN_PASSWORD", raising=False)
    monkeypatch.setenv("DFO_SPM_DEFAULT_ADMIN_PASSWORD_FILE", str(secret))

    assert bootstrap_admin_password("postgresql+psycopg://user:pass@db.example/dfo") == "StrongSecret!2026"


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


def test_login_rejects_cross_site_origin(tmp_path: Path):
    app = create_app(f"sqlite:///{tmp_path / 'login-csrf.db'}", seed=False)
    client = TestClient(app, follow_redirects=False)

    response = client.post(
        "/login",
        headers={"Origin": "https://attacker.example"},
        data={"username": "admin", "password": "admin123@"},
    )

    assert response.status_code == 403
    assert "set-cookie" not in response.headers


def test_login_origin_check_allows_tls_terminating_proxy_scheme_change(tmp_path: Path):
    app = create_app(f"sqlite:///{tmp_path / 'login-proxy.db'}", seed=False)
    client = TestClient(app, base_url="http://testserver", follow_redirects=False)

    response = client.post(
        "/login",
        headers={"Origin": "https://testserver"},
        data={"username": "admin", "password": "admin123@"},
    )

    assert response.status_code == 303
    assert "set-cookie" in response.headers


def test_login_allows_null_origin_only_for_same_origin_browser_navigation(tmp_path: Path):
    app = create_app(f"sqlite:///{tmp_path / 'login-null-origin.db'}", seed=False)
    client = TestClient(app, follow_redirects=False)

    allowed = client.post(
        "/login",
        headers={"Origin": "null", "Sec-Fetch-Site": "same-origin"},
        data={"username": "admin", "password": "admin123@"},
    )
    rejected = client.post(
        "/login",
        headers={"Origin": "null", "Sec-Fetch-Site": "cross-site"},
        data={"username": "admin", "password": "admin123@"},
    )

    assert allowed.status_code == 303
    assert "set-cookie" in allowed.headers
    assert rejected.status_code == 403
    assert "set-cookie" not in rejected.headers


def test_secure_cookie_mode_emits_hsts_header(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("DFO_SPM_COOKIE_SECURE", "true")
    app = create_app(f"sqlite:///{tmp_path / 'secure.db'}", seed=False)
    client = TestClient(app)

    assert client.get("/health").headers["strict-transport-security"] == "max-age=31536000; includeSubDomains"


def test_production_mode_refuses_insecure_cookies(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("DFO_SPM_ENV", "production")
    monkeypatch.setenv("DFO_SPM_COOKIE_SECURE", "false")
    monkeypatch.setenv("DFO_SPM_TRUSTED_HOSTS", "spm.example.test")

    with pytest.raises(RuntimeError, match="COOKIE_SECURE"):
        create_app(f"sqlite:///{tmp_path / 'insecure.db'}", seed=False)


def test_production_postgresql_requires_alembic_schema_management(monkeypatch):
    monkeypatch.setenv("DFO_SPM_ENV", "production")
    monkeypatch.setenv("DFO_SPM_COOKIE_SECURE", "true")
    monkeypatch.setenv("DFO_SPM_TRUSTED_HOSTS", "spm.example.test")
    monkeypatch.delenv("DFO_SPM_SCHEMA_MANAGEMENT", raising=False)

    with pytest.raises(RuntimeError, match="SCHEMA_MANAGEMENT"):
        validate_runtime_settings("postgresql+psycopg://user:pass@postgres/dfo")


def test_readiness_fails_when_alembic_schema_is_missing(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("DFO_SPM_SCHEMA_MANAGEMENT", "alembic")
    monkeypatch.setenv("DFO_SPM_BOOTSTRAP_ON_START", "false")
    app = create_app(f"sqlite:///{tmp_path / 'unmigrated.db'}", seed=False)

    response = TestClient(app).get("/ready")

    assert response.status_code == 503
    assert response.json() == {"status": "unavailable"}


def test_readiness_fails_when_attachment_storage_is_not_writable(tmp_path: Path, monkeypatch):
    attachment_dir = tmp_path / "read-only-attachments"
    attachment_dir.mkdir()
    attachment_dir.chmod(0o555)
    monkeypatch.setenv("DFO_SPM_ATTACHMENT_DIR", str(attachment_dir))
    try:
        app = create_app(f"sqlite:///{tmp_path / 'ready.db'}", seed=False)
        response = TestClient(app).get("/ready")
    finally:
        attachment_dir.chmod(0o755)

    assert response.status_code == 503
    assert response.json() == {"status": "unavailable"}


def test_readiness_fails_when_database_is_read_only(tmp_path: Path, monkeypatch):
    database = tmp_path / "read-only.db"
    create_app(f"sqlite:///{database}", seed=False)
    database.chmod(0o444)
    monkeypatch.setenv("DFO_SPM_ATTACHMENT_DIR", str(tmp_path / "attachments"))
    app = create_app(f"sqlite:///file:{database}?mode=ro&uri=true", seed=False)

    response = TestClient(app).get("/ready")

    assert response.status_code == 503
    assert response.json() == {"status": "unavailable"}


def test_production_mode_rejects_untrusted_hosts(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("DFO_SPM_ENV", "production")
    monkeypatch.setenv("DFO_SPM_COOKIE_SECURE", "true")
    monkeypatch.setenv("DFO_SPM_TRUSTED_HOSTS", "spm.example.test")
    app = create_app(f"sqlite:///{tmp_path / 'trusted-host.db'}", seed=False)

    assert TestClient(app, base_url="https://spm.example.test").get("/health").status_code == 200
    assert TestClient(app, base_url="https://evil.example.test").get("/health").status_code == 400


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


def test_login_throttling_does_not_lock_every_account_behind_one_address(tmp_path: Path):
    database_url = f"sqlite:///{tmp_path / 'throttle.db'}"
    app = create_app(database_url, seed=False)
    client = TestClient(app, follow_redirects=False)

    for attempt in range(5):
        assert client.post("/login", data={"username": f"unknown-{attempt}", "password": "wrong-password"}).status_code == 401

    allowed = client.post("/login", data={"username": "admin", "password": "admin123@"})
    assert allowed.status_code == 303


def test_login_is_throttled_per_account_across_source_addresses(tmp_path: Path):
    database_url = f"sqlite:///{tmp_path / 'account-throttle.db'}"
    app = create_app(database_url, seed=False)
    now = datetime.now(timezone.utc)
    with Session(create_engine(database_url)) as db:
        for attempt in range(5):
            db.add(AccessAudit(
                username="admin",
                action="LOGIN_FAILED",
                actor=f"198.51.100.{attempt}",
                detail="Invalid credentials.",
                timestamp=now,
            ))
        db.commit()

    blocked = TestClient(app, follow_redirects=False).post(
        "/login", data={"username": "admin", "password": "admin123@"}
    )
    assert blocked.status_code == 429
    assert blocked.headers["retry-after"] == "900"
