from __future__ import annotations

import hashlib
import json
import os
import secrets
import shutil
import zipfile
from datetime import date, timedelta
from io import BytesIO
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.trustedhost import TrustedHostMiddleware
from sqlalchemy import create_engine, func, inspect, select, text
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session, selectinload, sessionmaker

from .domain import (
    AE_CPERF_OPTIONAL_CRITERIA,
    AE_CPERF_WEIGHTS,
    AE_DEFAULT_WEIGHTS,
    CONSTRUCTION_CRITERIA,
    CONSTRUCTION_OPTIONAL_CRITERIA,
    ae_result,
    construction_result,
    supplier_risk_profile,
)
from .config import env
from .auth import hash_password, new_session_token, session_token_hash, validate_new_password, verify_password
from .exports import correspondence_pdf, performance_history_xlsx, tabular_report_pdf
from .models import AccessAudit, Attachment, AuditEntry, Base, Contract, DecisionAudit, Evaluation, EvaluationProjectDetails, EvaluationVersion, PerformanceDecision, Supplier, UserAccount, UserSession, utcnow
from .schemas import ContractCreate, DecisionCreate, EvaluationCreate, EvaluationUpdate, SupplierCreate, UserCreate, UserStatusUpdate, WorkflowActionRequest

BASE_DIR = Path(__file__).resolve().parent
TEMPLATES = Jinja2Templates(directory=str(BASE_DIR / "templates"))
EXPECTED_SCHEMA_REVISION = "e7a4b9c2d101"

WORKFLOW_TRANSITIONS = {
    ("DRAFT", "submit"): "SUBMITTED",
    ("RETURNED", "submit"): "SUBMITTED",
    ("SUBMITTED", "review"): "REVIEWED",
    ("REVIEWED", "approve"): "APPROVED",
    ("SUBMITTED", "return"): "RETURNED",
    ("REVIEWED", "return"): "RETURNED",
}
WORKFLOW_TRANSITION_ROLES = {
    ("DRAFT", "submit"): "EVALUATOR",
    ("RETURNED", "submit"): "EVALUATOR",
    ("SUBMITTED", "review"): "REVIEWER",
    ("SUBMITTED", "return"): "REVIEWER",
    ("REVIEWED", "approve"): "APPROVER",
    ("REVIEWED", "return"): "APPROVER",
}


def available_workflow_actions(status: str, roles: set[str]) -> list[str]:
    actions = [action for (source, action), _ in WORKFLOW_TRANSITIONS.items() if source == status]
    return [action for action in actions if "ADMIN" in roles or WORKFLOW_TRANSITION_ROLES[(status, action)] in roles]


def audit_actor(principal: dict) -> str:
    return f"{principal.get('name') or 'Authenticated User'} [{principal.get('username') or principal.get('id')}]"


PROJECT_DETAIL_FIELDS = (
    "client_reference_number", "description_of_work", "firm_address", "contractor_superintendent",
    "project_manager_name", "project_manager_telephone", "project_manager_fax", "project_manager_cell",
    "project_manager_email", "contract_award_amount", "contract_award_date", "final_amount",
    "contract_completion_date", "contract_changes_count", "final_certificate_date",
)


def project_details_dict(details: EvaluationProjectDetails | None) -> dict | None:
    if details is None:
        return None
    return {
        field: value.isoformat() if isinstance(value := getattr(details, field), date) else value
        for field in PROJECT_DETAIL_FIELDS
    }


def evaluation_dict(ev: Evaluation) -> dict:
    applicable_scores = [float(score) for score in (ev.scores or {}).values() if score is not None]
    raw_score = sum(applicable_scores)
    max_applicable_score = len(applicable_scores) * 20
    return {
        "id": ev.id,
        "contract_id": ev.contract_id,
        "model": ev.model,
        "evaluation_date": ev.evaluation_date.isoformat(),
        "due_date": ev.due_date.isoformat() if ev.due_date else None,
        "evaluator": ev.evaluator,
        "scores": ev.scores,
        "weights": ev.weights,
        "ratings": ev.ratings,
        "issues": ev.issues,
        "comments": ev.comments,
        "project_details": project_details_dict(ev.project_details),
        "total_score": ev.total_score,
        "raw_score": raw_score,
        "max_applicable_score": max_applicable_score,
        "applicable_criteria_count": len(applicable_scores),
        "outcome": ev.outcome,
        "status": ev.status,
        "version": ev.version,
    }


def contract_dict(contract: Contract) -> dict:
    return {
        "id": contract.id,
        "supplier_id": contract.supplier_id,
        "contract_number": contract.contract_number,
        "standing_offer_number": contract.standing_offer_number,
        "call_up_number": contract.call_up_number,
        "project_number": contract.project_number,
        "procurement_type": contract.procurement_type,
        "region": contract.region,
        "department": contract.department,
        "contract_value": contract.contract_value,
        "performance_evaluation_required": contract.performance_evaluation_required,
        "performance_regime": contract.performance_regime,
        "status": contract.status,
        "start_date": contract.start_date.isoformat() if contract.start_date else None,
        "end_date": contract.end_date.isoformat() if contract.end_date else None,
    }


def contract_summary(contracts: list[Contract]) -> dict[str, int]:
    active = sum(contract.status == "ACTIVE" for contract in contracts)
    return {"total": len(contracts), "active": active, "historical": len(contracts) - active}


def snapshot(ev: Evaluation) -> dict:
    return evaluation_dict(ev)


def validate_not_applicable_rationale(scores: dict[str, float | None], comments: str) -> None:
    if any(score is None for score in scores.values()) and len(comments.strip()) < 10:
        raise ValueError("Comments must explain each Not Applicable criterion (minimum 10 characters).")


def validate_project_details_for_model(model: str, details: dict | None) -> None:
    if model != "CONSTRUCTION" and details and (details.get("contractor_superintendent") or details.get("final_certificate_date")):
        raise ValueError("Contractor superintendent and final certificate date apply only to construction evaluations.")


def calculate(model: str, scores: dict[str, float | None], weights: dict[str, float] | None) -> tuple[dict, dict | None]:
    if model == "CONSTRUCTION":
        if set(scores) != set(CONSTRUCTION_CRITERIA):
            raise ValueError("Construction scores must include the five prescribed GC1.22 criteria.")
        invalid_na = {name for name, score in scores.items() if score is None} - CONSTRUCTION_OPTIONAL_CRITERIA
        if invalid_na:
            raise ValueError(f"These construction criteria cannot be Not Applicable: {', '.join(sorted(invalid_na))}.")
        result = construction_result([scores[name] for name in CONSTRUCTION_CRITERIA])
        result["ratings"] = {name: rating for name, rating in zip(CONSTRUCTION_CRITERIA, result["ratings"])}
        return result, None
    selected_weights = weights or (AE_CPERF_WEIGHTS if model == "AE_CPERF" else AE_DEFAULT_WEIGHTS)
    required = AE_CPERF_WEIGHTS if model == "AE_CPERF" else None
    if required and selected_weights != required:
        raise ValueError("The CPERF 2913-1 profile requires the prescribed Design, Quality of Results, Management, Time and Cost weights.")
    na_criteria = {name for name, score in scores.items() if score is None}
    if model == "AE_CPERF":
        invalid_na = na_criteria - AE_CPERF_OPTIONAL_CRITERIA
        if invalid_na:
            raise ValueError(f"These consultant CPERF criteria cannot be Not Applicable: {', '.join(sorted(invalid_na))}.")
    elif na_criteria:
        raise ValueError("The A&E extended profile requires a score for every weighted criterion.")
    return ae_result(scores, selected_weights), dict(selected_weights)


def secret_from_env(name: str) -> str | None:
    secret_file = env(f"{name}_FILE")
    if secret_file:
        try:
            value = Path(secret_file).read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise RuntimeError(f"Unable to read {name}_FILE.") from exc
        if not value:
            raise RuntimeError(f"{name}_FILE is empty.")
        return value
    return env(name) or None


def database_url_from_env() -> str:
    database_url = env("SPM_DATABASE_URL")
    if not database_url and env("SPM_DB_PASSWORD_FILE"):
        password = secret_from_env("SPM_DB_PASSWORD")
        user = env("SPM_DB_USER", "spm")
        host = env("SPM_DB_HOST", "postgres")
        port = env("SPM_DB_PORT", "5432")
        name = env("SPM_DB_NAME", "spm")
        if password:
            database_url = f"postgresql+psycopg://{quote(user, safe='')}:{quote(password, safe='')}@{host}:{port}/{quote(name, safe='')}"
    if not database_url:
        raise RuntimeError("SPM_DATABASE_URL (legacy DFO_SPM_DATABASE_URL) is required and must identify a PostgreSQL database.")
    if not database_url.startswith(("postgresql://", "postgresql+psycopg://")):
        raise RuntimeError("SPM_DATABASE_URL (legacy DFO_SPM_DATABASE_URL) must use PostgreSQL.")
    return database_url


def bootstrap_admin_password(database_url: str) -> str:
    password = secret_from_env("SPM_DEFAULT_ADMIN_PASSWORD")
    if not password and database_url.startswith("sqlite"):
        return "admin123@"
    if not password:
        raise RuntimeError("SPM_DEFAULT_ADMIN_PASSWORD (legacy DFO_SPM_DEFAULT_ADMIN_PASSWORD) is required when creating the first PostgreSQL administrator.")
    error = validate_new_password(password)
    if error:
        raise RuntimeError(f"SPM_DEFAULT_ADMIN_PASSWORD (legacy DFO_SPM_DEFAULT_ADMIN_PASSWORD) is invalid: {error}")
    return password


def validate_runtime_settings(database_url: str) -> None:
    if env("SPM_ENV", "development").lower() == "production":
        if env("SPM_COOKIE_SECURE", "false").lower() != "true":
            raise RuntimeError("SPM_COOKIE_SECURE (legacy DFO_SPM_COOKIE_SECURE) must be true in production.")
        if not env("SPM_TRUSTED_HOSTS", "").strip():
            raise RuntimeError("SPM_TRUSTED_HOSTS (legacy DFO_SPM_TRUSTED_HOSTS) is required in production.")
        if not database_url.startswith("sqlite") and env("SPM_SCHEMA_MANAGEMENT", "").lower() != "alembic":
            raise RuntimeError("SPM_SCHEMA_MANAGEMENT (legacy DFO_SPM_SCHEMA_MANAGEMENT) must be alembic for production PostgreSQL.")


def advisory_lock_key(value: str) -> int:
    return int.from_bytes(hashlib.blake2b(value.encode(), digest_size=8).digest(), "big", signed=True)


def locked_get(db: Session, model, record_id: int):
    return db.scalar(select(model).where(model.id == record_id).with_for_update())


def ensure_bootstrap_admin(session_factory, database_url: str) -> None:
    with session_factory() as db:
        if db.scalar(select(UserAccount).where(UserAccount.role == "ADMIN")):
            return
        admin_username = env("SPM_DEFAULT_ADMIN_USERNAME", "admin").strip().lower()
        admin_password = bootstrap_admin_password(database_url)
        db.add(UserAccount(username=admin_username, display_name="System Administrator", password_hash=hash_password(admin_password), role="ADMIN", must_change_password=True, created_by="SYSTEM"))
        db.add(AccessAudit(username=admin_username, action="ADMIN_BOOTSTRAPPED", actor="SYSTEM", detail="Forced password change enabled"))
        db.commit()


def create_app(database_url: str | None = None, seed: bool = False, auth_tokens: dict | None = None) -> FastAPI:
    database_url = database_url or database_url_from_env()
    validate_runtime_settings(database_url)
    connect_args = {"check_same_thread": False} if database_url.startswith("sqlite") else {}
    engine = create_engine(database_url, connect_args=connect_args, pool_pre_ping=True)
    SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
    if env("SPM_SCHEMA_MANAGEMENT", "application").lower() != "alembic":
        Base.metadata.create_all(engine)

    app = FastAPI(title="Supplier Performance Management System", version="1.0.0")
    trusted_hosts = [host.strip() for host in env("SPM_TRUSTED_HOSTS", "").split(",") if host.strip()]
    if trusted_hosts:
        app.add_middleware(TrustedHostMiddleware, allowed_hosts=trusted_hosts)
    app.state.engine = engine
    app.state.session_factory = SessionLocal
    upload_dir = Path(env("SPM_ATTACHMENT_DIR", str(BASE_DIR.parent / "data" / "attachments")))
    upload_dir.mkdir(parents=True, exist_ok=True)
    app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")

    def get_db():
        db = SessionLocal()
        try:
            yield db
        finally:
            db.close()

    def resolve_principal(request: Request, authorization: str | None = None) -> dict | None:
        token = authorization[7:] if authorization and authorization.startswith("Bearer ") else (request.cookies.get("spm_session") or request.cookies.get("dfo_spm_session"))
        if not token:
            return None
        now = utcnow()
        with SessionLocal() as db:
            session = db.scalar(select(UserSession).where(UserSession.token_hash == session_token_hash(token), UserSession.revoked_at.is_(None), UserSession.expires_at > now))
            if not session:
                return None
            account = db.get(UserAccount, session.user_id)
            if not account or not account.active:
                return None
            return {"id": account.id, "username": account.username, "name": account.display_name, "roles": account.role.split(","), "must_change_password": account.must_change_password}

    def authenticate(request: Request, authorization: str | None = Header(default=None)) -> dict:
        principal = getattr(request.state, "principal", None) or resolve_principal(request, authorization)
        if principal is None:
            raise HTTPException(401, "Authentication is required.", headers={"WWW-Authenticate": "Bearer"})
        if principal["must_change_password"]:
            raise HTTPException(403, "Password change required before accessing application data.")
        return principal

    def require_roles(*allowed: str):
        def dependency(principal: dict = Depends(authenticate)) -> str:
            if not set(principal["roles"]).intersection(allowed):
                raise HTTPException(403, "Insufficient role for this action.")
            return audit_actor(principal)
        return dependency

    def is_assigned_evaluator(ev: Evaluation, principal: dict, db: Session) -> bool:
        if "ADMIN" in principal["roles"]:
            return True
        originator = db.scalar(
            select(EvaluationVersion.user)
            .where(EvaluationVersion.evaluation_id == ev.id)
            .order_by(EvaluationVersion.version)
            .limit(1)
        )
        return originator == audit_actor(principal)

    def require_assigned_evaluator(ev: Evaluation, request: Request, db: Session) -> None:
        if not is_assigned_evaluator(ev, request.state.principal, db):
            raise HTTPException(403, "Only the assigned evaluator may revise or resubmit this evaluation.")

    @app.middleware("http")
    async def security_boundary(request: Request, call_next):
        public = request.url.path in {"/health", "/ready", "/login"} or request.url.path.startswith("/static/")
        principal = None if public else resolve_principal(request, request.headers.get("Authorization"))
        request.state.principal = principal
        if not public and principal is None:
            if request.url.path.startswith("/api/"):
                return JSONResponse({"detail": "Authentication required."}, status_code=401, headers={"WWW-Authenticate": "Bearer"})
            return RedirectResponse("/login", status_code=303)
        if principal and principal["must_change_password"] and request.url.path not in {"/change-password", "/logout"}:
            if request.url.path.startswith("/api/"):
                return JSONResponse({"detail": "Password change required."}, status_code=403)
            return RedirectResponse("/change-password", status_code=303)
        response = await call_next(request)
        response.headers["Content-Security-Policy"] = "default-src 'self'; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; font-src https://fonts.gstatic.com; script-src 'self'; object-src 'none'; base-uri 'self'; frame-ancestors 'none'; form-action 'self'"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        response.headers["Cache-Control"] = "no-store"
        if env("SPM_COOKIE_SECURE", "false").lower() == "true":
            response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        return response

    @app.get("/login", response_class=HTMLResponse)
    def login_page(request: Request):
        return TEMPLATES.TemplateResponse(request, "login.html", {"error": None})

    @app.post("/login")
    def login(request: Request, username: str = Form(...), password: str = Form(...), db: Session = Depends(get_db)):
        origin = request.headers.get("Origin")
        if origin:
            if origin == "null":
                same_origin = request.headers.get("Sec-Fetch-Site") == "same-origin"
            else:
                parsed_origin = urlsplit(origin)
                same_origin = parsed_origin.scheme in {"http", "https"} and parsed_origin.hostname == request.url.hostname
            if not same_origin:
                raise HTTPException(403, "Cross-site sign-in is not permitted.")
        normalized = username.strip().lower()
        source = request.client.host if request.client else "unknown"
        source_key = source[:200]
        source_detail = f"source={source}"
        throttle_window = int(env("SPM_LOGIN_THROTTLE_SECONDS", "900"))
        account_max_attempts = int(env("SPM_LOGIN_MAX_ATTEMPTS", "5"))
        source_max_attempts = int(env("SPM_LOGIN_SOURCE_MAX_ATTEMPTS", "50"))
        if db.bind is not None and db.bind.dialect.name == "postgresql":
            lock_keys = sorted({advisory_lock_key(f"account:{normalized}"), advisory_lock_key(f"source:{source_key}")})
            for lock_key in lock_keys:
                db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": lock_key})
        window_start = utcnow() - timedelta(seconds=throttle_window)
        recent_account_failures = db.scalar(
            select(func.count(AccessAudit.id)).where(
                AccessAudit.action == "LOGIN_FAILED",
                AccessAudit.username == normalized[:80],
                AccessAudit.timestamp >= window_start,
            )
        ) or 0
        recent_source_failures = db.scalar(
            select(func.count(AccessAudit.id)).where(
                AccessAudit.action == "LOGIN_FAILED",
                AccessAudit.actor == source_key,
                AccessAudit.timestamp >= window_start,
            )
        ) or 0
        if recent_account_failures >= account_max_attempts or recent_source_failures >= source_max_attempts:
            return TEMPLATES.TemplateResponse(
                request,
                "login.html",
                {"error": "Too many sign-in attempts. Try again later."},
                status_code=429,
                headers={"Retry-After": str(throttle_window)},
            )
        account = db.scalar(select(UserAccount).where(UserAccount.username == normalized))
        if not account or not account.active or not verify_password(password, account.password_hash):
            db.add(AccessAudit(username=normalized[:80], action="LOGIN_FAILED", actor=source_key, detail=source_detail))
            db.commit()
            return TEMPLATES.TemplateResponse(request, "login.html", {"error": "Invalid username or password."}, status_code=401)
        token = new_session_token()
        account.last_login_at = utcnow()
        db.add(UserSession(user_id=account.id, token_hash=session_token_hash(token), expires_at=utcnow() + timedelta(hours=8)))
        db.add(AccessAudit(username=account.username, action="LOGIN_SUCCESS", actor=account.display_name))
        db.commit()
        destination = "/change-password" if account.must_change_password else "/"
        response = RedirectResponse(destination, status_code=303)
        response.set_cookie("spm_session", token, httponly=True, secure=env("SPM_COOKIE_SECURE", "false").lower() == "true", samesite="strict", max_age=28800)
        return response

    @app.get("/change-password", response_class=HTMLResponse)
    def change_password_page(request: Request):
        return TEMPLATES.TemplateResponse(request, "change_password.html", {"error": None})

    @app.post("/change-password")
    def change_password(request: Request, current_password: str = Form(...), new_password: str = Form(...), confirm_password: str = Form(...), db: Session = Depends(get_db)):
        principal = request.state.principal
        account = db.get(UserAccount, principal["id"])
        if not account or not account.active:
            raise HTTPException(401, "Account is no longer available.")
        error = validate_new_password(new_password)
        if not verify_password(current_password, account.password_hash):
            error = "Current password is incorrect."
        elif new_password != confirm_password:
            error = "New password and confirmation do not match."
        elif verify_password(new_password, account.password_hash):
            error = "New password must differ from the current password."
        if error:
            return TEMPLATES.TemplateResponse(request, "change_password.html", {"error": error}, status_code=422)
        account.password_hash = hash_password(new_password)
        account.must_change_password = False
        account.password_changed_at = utcnow()
        for active_session in db.scalars(select(UserSession).where(UserSession.user_id == account.id, UserSession.revoked_at.is_(None))).all():
            active_session.revoked_at = utcnow()
        replacement_token = new_session_token()
        db.add(UserSession(user_id=account.id, token_hash=session_token_hash(replacement_token), expires_at=utcnow() + timedelta(hours=8)))
        db.add(AccessAudit(username=account.username, action="PASSWORD_CHANGED", actor=account.display_name, detail="All prior sessions revoked"))
        db.commit()
        response = RedirectResponse("/", status_code=303)
        response.set_cookie("spm_session", replacement_token, httponly=True, secure=env("SPM_COOKIE_SECURE", "false").lower() == "true", samesite="strict", max_age=28800)
        return response

    @app.post("/logout")
    def logout(request: Request, db: Session = Depends(get_db)):
        tokens = {token for name in ("spm_session", "dfo_spm_session") if (token := request.cookies.get(name))}
        for token in tokens:
            session = db.scalar(select(UserSession).where(UserSession.token_hash == session_token_hash(token), UserSession.revoked_at.is_(None)))
            if session:
                session.revoked_at = utcnow()
        db.commit()
        response = RedirectResponse("/login", status_code=303)
        response.delete_cookie("spm_session")
        response.delete_cookie("dfo_spm_session")
        return response

    if env("SPM_BOOTSTRAP_ON_START", "true").lower() == "true":
        ensure_bootstrap_admin(SessionLocal, database_url)

    with SessionLocal() as db:
        for index, (token, principal) in enumerate((auth_tokens or {}).items(), start=1):
            username = f"test-user-{index}"
            account = db.scalar(select(UserAccount).where(UserAccount.username == username))
            if not account:
                account = UserAccount(username=username, display_name=str(principal.get("name", username)), password_hash=hash_password(secrets.token_urlsafe(24)), role=",".join(principal.get("roles", [])), must_change_password=False, created_by="TEST_BOOTSTRAP")
                db.add(account)
                db.flush()
            if not db.scalar(select(UserSession).where(UserSession.token_hash == session_token_hash(str(token)))):
                db.add(UserSession(user_id=account.id, token_hash=session_token_hash(str(token)), expires_at=utcnow() + timedelta(days=365)))
        db.commit()

    if seed:
        with SessionLocal() as db:
            seed_demo(db)

    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.get("/ready")
    def ready():
        try:
            with engine.connect() as connection:
                connection.execute(text("SELECT 1"))
                if env("SPM_SCHEMA_MANAGEMENT", "application").lower() == "alembic":
                    tables = set(inspect(connection).get_table_names())
                    if not set(Base.metadata.tables).issubset(tables) or "alembic_version" not in tables:
                        raise SQLAlchemyError("Required migrated schema is unavailable.")
                    revision = connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one_or_none()
                    if revision != EXPECTED_SCHEMA_REVISION:
                        raise SQLAlchemyError("Required schema revision is unavailable.")
                connection.execute(text("UPDATE user_sessions SET token_hash = token_hash WHERE 1 = 0"))
            probe_path = upload_dir / f".readiness-{secrets.token_hex(8)}"
            try:
                with probe_path.open("xb") as probe:
                    probe.write(b"ready")
                    probe.flush()
                    os.fsync(probe.fileno())
            finally:
                probe_path.unlink(missing_ok=True)
        except (OSError, SQLAlchemyError):
            return JSONResponse({"status": "unavailable"}, status_code=503)
        return {"status": "ready"}

    @app.get("/api/admin/users")
    def list_users(admin: str = Depends(require_roles("ADMIN")), db: Session = Depends(get_db)):
        users = db.scalars(select(UserAccount).order_by(UserAccount.username)).all()
        return [{"id": user.id, "username": user.username, "display_name": user.display_name, "role": user.role, "active": user.active, "must_change_password": user.must_change_password, "last_login_at": user.last_login_at.isoformat() if user.last_login_at else None} for user in users]

    @app.post("/api/admin/users", status_code=201)
    def create_user(payload: UserCreate, admin: str = Depends(require_roles("ADMIN")), db: Session = Depends(get_db)):
        error = validate_new_password(payload.temporary_password)
        if error:
            raise HTTPException(422, error)
        user = UserAccount(username=payload.username.lower(), display_name=payload.display_name, password_hash=hash_password(payload.temporary_password), role=payload.role, must_change_password=True, created_by=admin)
        db.add(user)
        try:
            db.flush()
        except IntegrityError:
            db.rollback()
            raise HTTPException(409, "Username already exists.")
        db.add(AccessAudit(username=user.username, action="USER_CREATED", actor=admin, detail=f"Role: {user.role}"))
        db.commit()
        db.refresh(user)
        return {"id": user.id, "username": user.username, "display_name": user.display_name, "role": user.role, "active": user.active, "must_change_password": user.must_change_password}

    @app.patch("/api/admin/users/{user_id}")
    def update_user_status(user_id: int, payload: UserStatusUpdate, admin: str = Depends(require_roles("ADMIN")), db: Session = Depends(get_db)):
        user = db.get(UserAccount, user_id)
        if not user:
            raise HTTPException(404, "User not found.")
        if user.role == "ADMIN" and not payload.active:
            raise HTTPException(409, "The bootstrap administrator cannot be deactivated through this endpoint.")
        user.active = payload.active
        if not payload.active:
            for session in db.scalars(select(UserSession).where(UserSession.user_id == user.id, UserSession.revoked_at.is_(None))).all():
                session.revoked_at = utcnow()
        db.add(AccessAudit(username=user.username, action="USER_ACTIVATED" if payload.active else "USER_DEACTIVATED", actor=admin))
        db.commit()
        return {"id": user.id, "active": user.active}

    @app.get("/admin/users", response_class=HTMLResponse)
    def users_page(request: Request, admin: str = Depends(require_roles("ADMIN")), db: Session = Depends(get_db)):
        users = db.scalars(select(UserAccount).order_by(UserAccount.username)).all()
        return TEMPLATES.TemplateResponse(request, "admin_users.html", {"users": users})

    @app.post("/api/suppliers", status_code=201)
    def create_supplier(payload: SupplierCreate, user: str = Depends(require_roles("ADMIN")), db: Session = Depends(get_db)):
        supplier = Supplier(**payload.model_dump())
        db.add(supplier)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            raise HTTPException(409, "A supplier with this name already exists.")
        db.refresh(supplier)
        return {"id": supplier.id, "name": supplier.name, "business_number": supplier.business_number, "status": supplier.status}

    @app.get("/api/suppliers")
    def list_suppliers(db: Session = Depends(get_db)):
        suppliers = db.scalars(select(Supplier).order_by(Supplier.name)).all()
        return [{"id": item.id, "name": item.name, "business_number": item.business_number, "status": item.status} for item in suppliers]

    @app.post("/api/contracts", status_code=201)
    def create_contract(payload: ContractCreate, user: str = Depends(require_roles("ADMIN")), db: Session = Depends(get_db)):
        if not db.get(Supplier, payload.supplier_id):
            raise HTTPException(404, "Supplier not found.")
        data = payload.model_dump()
        if data["performance_regime"] == "APPLICABLE_CONTRACT_TERMS":
            construction_types = {"Construction", "Construction Standing Offer", "Construction Call-Up"}
            data["performance_regime"] = "GI16_GC1_22" if data["procurement_type"] in construction_types else "AE_EXTENDED"
        contract = Contract(**data)
        db.add(contract)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            raise HTTPException(409, "Contract number already exists.")
        db.refresh(contract)
        return contract_dict(contract)

    @app.get("/api/contracts")
    def list_contracts(db: Session = Depends(get_db)):
        return [contract_dict(item) for item in db.scalars(select(Contract).order_by(Contract.contract_number)).all()]

    @app.post("/api/evaluations", status_code=201)
    def create_evaluation(payload: EvaluationCreate, user: str = Depends(require_roles("EVALUATOR")), db: Session = Depends(get_db)):
        contract = db.get(Contract, payload.contract_id) if payload.contract_id is not None else None
        if payload.contract_id is not None and not contract:
            raise HTTPException(404, "Contract not found.")
        if payload.supplier_id is not None and not db.get(Supplier, payload.supplier_id):
            raise HTTPException(404, "Supplier not found.")
        if contract is None:
            assert payload.new_contract is not None
            contract_details = payload.new_contract
        else:
            contract_details = contract
        if not contract_details.performance_evaluation_required:
            raise HTTPException(409, "This contract is not designated for performance evaluation under its recorded contractual regime.")
        compatible_models = {
            "GI16_GC1_22": {"CONSTRUCTION"},
            "GI23_GC26_2913_1": {"AE_CPERF"},
            "AE_EXTENDED": {"AE"},
            "DFO_AE_EXTENDED": {"AE"},  # Existing contracts retain their historical regime.
        }
        if payload.model not in compatible_models.get(contract_details.performance_regime, set()):
            raise HTTPException(422, f"Evaluation model {payload.model} is incompatible with contract regime {contract_details.performance_regime}.")
        try:
            validate_not_applicable_rationale(payload.scores, payload.comments)
            validate_project_details_for_model(payload.model, payload.project_details.model_dump() if payload.project_details else None)
            result, resolved_weights = calculate(payload.model, payload.scores, payload.weights)
        except ValueError as exc:
            raise HTTPException(422, str(exc))
        created_supplier = None
        try:
            if contract is None:
                if payload.new_supplier is not None:
                    created_supplier = Supplier(**payload.new_supplier.model_dump())
                    db.add(created_supplier)
                    db.flush()
                    supplier_id = created_supplier.id
                else:
                    supplier_id = payload.supplier_id
                contract = Contract(supplier_id=supplier_id, **payload.new_contract.model_dump())
                db.add(contract)
                db.flush()
            ev = Evaluation(
                contract_id=contract.id,
                model=payload.model,
                evaluation_date=payload.evaluation_date,
                due_date=payload.due_date,
                evaluator=payload.evaluator,
                scores=payload.scores,
                weights=resolved_weights,
                ratings=result["ratings"],
                issues=payload.issues,
                comments=payload.comments,
                total_score=result["percentage"],
                outcome=result["outcome"],
                status="DRAFT",
                version=1,
            )
            db.add(ev)
            db.flush()
            if payload.project_details:
                ev.project_details = EvaluationProjectDetails(**payload.project_details.model_dump())
                db.flush()
            db.add(EvaluationVersion(evaluation_id=ev.id, version=1, snapshot=snapshot(ev), user=user))
            if created_supplier:
                db.add(AuditEntry(evaluation_id=ev.id, user=user, action="CREATE_SUPPLIER_WITH_EVALUATION", field_name="supplier", original_value=None, revised_value=json.dumps({"id": created_supplier.id, "name": created_supplier.name, "business_number": created_supplier.business_number})))
            if payload.new_contract is not None:
                db.add(AuditEntry(evaluation_id=ev.id, user=user, action="CREATE_CONTRACT_WITH_EVALUATION", field_name="contract", original_value=None, revised_value=json.dumps(contract_dict(contract), default=str)))
            db.add(AuditEntry(evaluation_id=ev.id, user=user, action="CREATE", field_name=None, original_value=None, revised_value=json.dumps(snapshot(ev), default=str)))
            db.commit()
        except IntegrityError:
            db.rollback()
            raise HTTPException(409, "Supplier name or contract number already exists. Select the existing record or use unique identifiers.")
        db.refresh(ev)
        return evaluation_dict(ev)

    @app.get("/api/evaluations")
    def list_evaluations(db: Session = Depends(get_db)):
        return [evaluation_dict(item) for item in db.scalars(select(Evaluation).order_by(Evaluation.evaluation_date.desc())).all()]

    @app.get("/api/evaluations/{evaluation_id}")
    def get_evaluation(evaluation_id: int, db: Session = Depends(get_db)):
        ev = db.get(Evaluation, evaluation_id)
        if not ev:
            raise HTTPException(404, "Evaluation not found.")
        data = evaluation_dict(ev)
        data["contract"] = contract_dict(ev.contract)
        data["supplier"] = {"id": ev.contract.supplier.id, "name": ev.contract.supplier.name}
        return data

    @app.patch("/api/evaluations/{evaluation_id}")
    def update_evaluation(evaluation_id: int, payload: EvaluationUpdate, request: Request, user: str = Depends(require_roles("EVALUATOR")), db: Session = Depends(get_db)):
        ev = locked_get(db, Evaluation, evaluation_id)
        if not ev:
            raise HTTPException(404, "Evaluation not found.")
        if ev.status not in {"DRAFT", "RETURNED"}:
            raise HTTPException(409, "Only draft or returned evaluations may be edited. Return the evaluation before revising it.")
        require_assigned_evaluator(ev, request, db)
        changes = payload.model_dump(exclude_unset=True)
        if not changes:
            return evaluation_dict(ev)
        project_details = changes.pop("project_details", None)
        proposed_date = changes.get("evaluation_date", ev.evaluation_date)
        proposed_due = changes.get("due_date", ev.due_date)
        if proposed_due and proposed_due < proposed_date:
            raise HTTPException(422, "Evaluation due date cannot precede evaluation date.")
        prospective_scores = changes.get("scores", ev.scores)
        prospective_weights = changes.get("weights", ev.weights)
        prospective_comments = changes.get("comments", ev.comments)
        try:
            validate_not_applicable_rationale(prospective_scores, prospective_comments)
            validate_project_details_for_model(ev.model, project_details)
            result, resolved_weights = calculate(ev.model, prospective_scores, prospective_weights)
        except ValueError as exc:
            raise HTTPException(422, str(exc))
        if "scores" in changes or "weights" in changes:
            changes.update({"weights": resolved_weights, "ratings": result["ratings"], "total_score": result["percentage"], "outcome": result["outcome"]})
        changed = False
        if project_details is not None:
            original_details = project_details_dict(ev.project_details)
            if ev.project_details is None:
                ev.project_details = EvaluationProjectDetails(**project_details)
            else:
                for field in PROJECT_DETAIL_FIELDS:
                    setattr(ev.project_details, field, project_details.get(field))
            revised_details = project_details_dict(ev.project_details)
            if original_details != revised_details:
                changed = True
                db.add(AuditEntry(evaluation_id=ev.id, user=user, action="UPDATE", field_name="project_details", original_value=json.dumps(original_details, default=str), revised_value=json.dumps(revised_details, default=str)))
        for field, revised in changes.items():
            original = getattr(ev, field)
            if original != revised:
                changed = True
                db.add(AuditEntry(evaluation_id=ev.id, user=user, action="UPDATE", field_name=field, original_value=json.dumps(original, default=str), revised_value=json.dumps(revised, default=str)))
                setattr(ev, field, revised)
        if not changed:
            return evaluation_dict(ev)
        ev.version += 1
        db.flush()
        db.add(EvaluationVersion(evaluation_id=ev.id, version=ev.version, snapshot=snapshot(ev), user=user))
        db.commit()
        db.refresh(ev)
        return evaluation_dict(ev)

    @app.post("/api/evaluations/{evaluation_id}/workflow/{action}")
    def workflow(evaluation_id: int, action: str, payload: WorkflowActionRequest | None = None, principal: dict = Depends(authenticate), db: Session = Depends(get_db)):
        roles = set(principal.get("roles", []))
        ev = locked_get(db, Evaluation, evaluation_id)
        if not ev:
            raise HTTPException(404, "Evaluation not found.")
        normalized_action = action.lower()
        revised = WORKFLOW_TRANSITIONS.get((ev.status, normalized_action))
        if not revised:
            raise HTTPException(409, f"Action '{action}' is not valid from status {ev.status}.")
        required = WORKFLOW_TRANSITION_ROLES[(ev.status, normalized_action)]
        if "ADMIN" not in roles and required not in roles:
            raise HTTPException(403, "Insufficient role for this workflow action.")
        if normalized_action == "submit" and not is_assigned_evaluator(ev, principal, db):
            raise HTTPException(403, "Only the assigned evaluator may revise or resubmit this evaluation.")
        return_comment = (payload.comment or "").strip() if payload else ""
        if normalized_action == "return" and len(return_comment) < 10:
            raise HTTPException(422, "A return comment of at least 10 characters is required so the evaluator can revise the evaluation.")
        user = audit_actor(principal)
        prior_workflow = db.scalars(select(AuditEntry).where(AuditEntry.evaluation_id == evaluation_id, AuditEntry.action.in_(["WORKFLOW_SUBMIT", "WORKFLOW_REVIEW"]))).all()
        if normalized_action == "review" and any(a.action == "WORKFLOW_SUBMIT" and a.user == user for a in prior_workflow):
            raise HTTPException(409, "The submitting user cannot review the same evaluation.")
        if normalized_action == "approve" and any(a.user == user for a in prior_workflow):
            raise HTTPException(409, "The approver must be distinct from the submitting and reviewing users.")
        original = ev.status
        ev.status = revised
        ev.version += 1
        db.flush()
        db.add(AuditEntry(evaluation_id=ev.id, user=user, action=f"WORKFLOW_{action.upper()}", field_name="status", original_value=original, revised_value=revised))
        if normalized_action == "return":
            db.add(AuditEntry(evaluation_id=ev.id, user=user, action="WORKFLOW_RETURN_COMMENT", field_name="return_comment", original_value=None, revised_value=json.dumps(return_comment)))
        db.add(EvaluationVersion(evaluation_id=ev.id, version=ev.version, snapshot=snapshot(ev), user=user))
        db.commit()
        db.refresh(ev)
        return evaluation_dict(ev)

    @app.get("/api/evaluations/{evaluation_id}/history")
    def evaluation_history(evaluation_id: int, db: Session = Depends(get_db)):
        if not db.get(Evaluation, evaluation_id):
            raise HTTPException(404, "Evaluation not found.")
        versions = db.scalars(select(EvaluationVersion).where(EvaluationVersion.evaluation_id == evaluation_id).order_by(EvaluationVersion.version)).all()
        audit = db.scalars(select(AuditEntry).where(AuditEntry.evaluation_id == evaluation_id).order_by(AuditEntry.timestamp)).all()
        return {
            "versions": [{"version": v.version, "snapshot": v.snapshot, "user": v.user, "timestamp": v.timestamp.isoformat()} for v in versions],
            "audit": [{"action": a.action, "field_name": a.field_name, "original_value": a.original_value, "revised_value": a.revised_value, "user": a.user, "timestamp": a.timestamp.isoformat()} for a in audit],
            "return_comments": [{"comment": json.loads(a.revised_value or '""'), "user": a.user, "timestamp": a.timestamp.isoformat()} for a in audit if a.action == "WORKFLOW_RETURN_COMMENT"],
        }

    @app.post("/api/evaluations/{evaluation_id}/attachments", status_code=201)
    async def upload_attachment(evaluation_id: int, request: Request, file: UploadFile = File(...), user: str = Depends(require_roles("EVALUATOR")), db: Session = Depends(get_db)):
        ev = locked_get(db, Evaluation, evaluation_id)
        if not ev:
            raise HTTPException(404, "Evaluation not found.")
        if ev.status not in {"DRAFT", "RETURNED"}:
            raise HTTPException(409, "Evidence may be added only while an evaluation is draft or returned.")
        require_assigned_evaluator(ev, request, db)
        safe_name = Path(file.filename or "evidence.bin").name
        if len(safe_name) > 180:
            raise HTTPException(422, "Attachment filename exceeds 180 characters.")
        allowed_types = {"application/pdf", "image/png", "image/jpeg", "text/plain", "application/vnd.openxmlformats-officedocument.wordprocessingml.document", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"}
        if file.content_type not in allowed_types:
            raise HTTPException(415, "Attachment type is not permitted.")
        digestor = hashlib.sha256()
        size = 0
        temp_path = upload_dir / f".{secrets.token_hex(16)}.part"
        try:
            with temp_path.open("xb") as target:
                while chunk := await file.read(1024 * 1024):
                    size += len(chunk)
                    if size > 20 * 1024 * 1024:
                        raise HTTPException(413, "Attachment exceeds the 20 MB limit.")
                    digestor.update(chunk)
                    target.write(chunk)
            prefix = temp_path.read_bytes()[:8]
            valid_signature = (
                (file.content_type == "application/pdf" and prefix.startswith(b"%PDF")) or
                (file.content_type == "image/png" and prefix.startswith(b"\x89PNG\r\n\x1a\n")) or
                (file.content_type == "image/jpeg" and prefix.startswith(b"\xff\xd8\xff")) or
                file.content_type == "text/plain"
            )
            if file.content_type in {"application/vnd.openxmlformats-officedocument.wordprocessingml.document", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"}:
                try:
                    with zipfile.ZipFile(temp_path) as archive:
                        names = set(archive.namelist())
                    valid_signature = "[Content_Types].xml" in names and ((file.content_type.endswith("wordprocessingml.document") and any(n.startswith("word/") for n in names)) or (file.content_type.endswith("spreadsheetml.sheet") and any(n.startswith("xl/") for n in names)))
                except zipfile.BadZipFile:
                    valid_signature = False
            if file.content_type == "text/plain":
                try:
                    temp_path.read_text(encoding="utf-8")
                except UnicodeDecodeError:
                    valid_signature = False
            if not valid_signature:
                raise HTTPException(415, "Attachment content does not match its declared permitted type.")
            digest = digestor.hexdigest()
            stored = f"{evaluation_id}_{secrets.token_hex(8)}_{safe_name}"
            final_path = upload_dir / stored
            temp_path.replace(final_path)
            item = Attachment(evaluation_id=evaluation_id, original_filename=safe_name, stored_filename=stored, content_type=file.content_type, sha256=digest, uploaded_by=user)
            db.add(item)
            db.flush()
            db.add(AuditEntry(evaluation_id=evaluation_id, user=user, action="ATTACHMENT_ADD", field_name="attachment", original_value=None, revised_value=f"{safe_name} sha256:{digest}"))
            db.commit()
        except Exception:
            db.rollback()
            temp_path.unlink(missing_ok=True)
            if "final_path" in locals():
                final_path.unlink(missing_ok=True)
            raise
        return {"id": item.id, "filename": safe_name, "sha256": digest}

    @app.get("/api/suppliers/{supplier_id}/profile")
    def supplier_profile(supplier_id: int, db: Session = Depends(get_db)):
        supplier = db.scalar(select(Supplier).where(Supplier.id == supplier_id).options(selectinload(Supplier.contracts).selectinload(Contract.evaluations)))
        if not supplier:
            raise HTTPException(404, "Supplier not found.")
        approved = [ev for c in supplier.contracts for ev in c.evaluations if ev.status == "APPROVED"]
        risk = supplier_risk_profile([{"score": ev.total_score, "date": ev.evaluation_date.isoformat(), "issues": ev.issues, "outcome": ev.outcome} for ev in approved])
        today = date.today()
        decisions = db.scalars(select(PerformanceDecision).where(PerformanceDecision.supplier_id == supplier_id).order_by(PerformanceDecision.effective_date.desc())).all()
        active_suspensions = [d for d in decisions if d.decision_type == "SUSPENSION" and d.status == "APPROVED" and d.effective_date <= today and (d.expiry_date is None or d.expiry_date >= today)]
        summary = contract_summary(supplier.contracts)
        return {
            "supplier": {"id": supplier.id, "name": supplier.name, "business_number": supplier.business_number},
            "contract_summary": summary,
            "active_contracts": [contract_dict(c) for c in supplier.contracts if c.status == "ACTIVE"],
            "historical_contracts": [contract_dict(c) for c in supplier.contracts if c.status != "ACTIVE"],
            "evaluations": [evaluation_dict(ev) for ev in sorted(approved, key=lambda x: x.evaluation_date, reverse=True)],
            "warning_letters": sum(ev.outcome == "WARNING" for ev in approved),
            "suspension_recommendations": sum(ev.outcome == "SUSPENSION_RECOMMENDATION" for ev in approved),
            "active_suspension_decisions": [{"id": d.id, "effective_date": d.effective_date.isoformat(), "expiry_date": d.expiry_date.isoformat() if d.expiry_date else None, "authority": d.authority, "rationale": d.rationale} for d in active_suspensions],
            "decisions": [{"id": d.id, "type": d.decision_type, "status": d.status, "effective_date": d.effective_date.isoformat(), "expiry_date": d.expiry_date.isoformat() if d.expiry_date else None, "authority": d.authority} for d in decisions],
            "risk": risk,
        }

    @app.post("/api/decisions", status_code=201)
    def create_decision(payload: DecisionCreate, user: str = Depends(require_roles("EVALUATOR", "DECISION_MAKER")), db: Session = Depends(get_db)):
        if not db.get(Supplier, payload.supplier_id):
            raise HTTPException(404, "Supplier not found.")
        if payload.evaluation_id:
            evaluation = db.get(Evaluation, payload.evaluation_id)
            if not evaluation or evaluation.contract.supplier_id != payload.supplier_id:
                raise HTTPException(422, "The evaluation must belong to the selected supplier.")
            if evaluation.status != "APPROVED":
                raise HTTPException(409, "A performance decision may reference only an approved evaluation.")
            if payload.decision_type == "SUSPENSION" and evaluation.outcome != "SUSPENSION_RECOMMENDATION":
                raise HTTPException(422, "A suspension decision requires an approved suspension recommendation.")
        if payload.expiry_date and payload.expiry_date < payload.effective_date:
            raise HTTPException(422, "Decision expiry date cannot precede its effective date.")
        if payload.status != "PENDING":
            raise HTTPException(422, "New decisions must start as PENDING and be approved through the decision workflow.")
        item = PerformanceDecision(**payload.model_dump(), recorded_by=user)
        db.add(item)
        db.flush()
        decision_snapshot = payload.model_dump(mode="json") | {"id": item.id, "recorded_by": user}
        db.add(DecisionAudit(decision_id=item.id, user=user, action="CREATE", original_status=None, revised_status="PENDING", snapshot=decision_snapshot))
        if payload.evaluation_id:
            db.add(AuditEntry(evaluation_id=payload.evaluation_id, user=user, action="PERFORMANCE_DECISION", field_name="decision", original_value=None, revised_value=json.dumps(payload.model_dump(), default=str)))
        db.commit()
        db.refresh(item)
        return {"id": item.id, "supplier_id": item.supplier_id, "evaluation_id": item.evaluation_id, "decision_type": item.decision_type, "status": item.status, "effective_date": item.effective_date.isoformat(), "expiry_date": item.expiry_date.isoformat() if item.expiry_date else None, "authority": item.authority}

    @app.post("/api/decisions/{decision_id}/approve")
    def approve_decision(decision_id: int, user: str = Depends(require_roles("DECISION_MAKER")), db: Session = Depends(get_db)):
        item = locked_get(db, PerformanceDecision, decision_id)
        if not item:
            raise HTTPException(404, "Decision not found.")
        if item.status != "PENDING":
            raise HTTPException(409, "Only pending decisions may be approved.")
        if item.recorded_by == user:
            raise HTTPException(409, "Segregation of duties prohibits a decision originator from approving the same decision.")
        if item.decision_type == "SUSPENSION" and not all([item.notice_date, item.representation_deadline, item.representations_summary, item.legal_review_reference, item.delegated_authority_reference]):
            raise HTTPException(409, "Suspension approval requires notice, representations, legal review and delegated-authority evidence.")
        item.status = "APPROVED"
        item.authority = user
        db.add(DecisionAudit(decision_id=item.id, user=user, action="APPROVE", original_status="PENDING", revised_status="APPROVED", snapshot={"id": item.id, "decision_type": item.decision_type, "status": "APPROVED", "authority": user, "effective_date": item.effective_date.isoformat(), "expiry_date": item.expiry_date.isoformat() if item.expiry_date else None}))
        if item.evaluation_id:
            db.add(AuditEntry(evaluation_id=item.evaluation_id, user=user, action="DECISION_APPROVE", field_name="decision_status", original_value="PENDING", revised_value="APPROVED"))
        db.commit()
        db.refresh(item)
        return {"id": item.id, "status": item.status, "authority": item.authority}

    @app.get("/api/dashboard")
    def dashboard(db: Session = Depends(get_db)):
        suppliers = db.scalars(select(Supplier)).all()
        contracts = db.scalars(select(Contract)).all()
        evaluations = db.scalars(select(Evaluation).where(Evaluation.status == "APPROVED")).all()
        profiles = []
        for supplier in suppliers:
            supplier_evals = [ev for ev in evaluations if ev.contract.supplier_id == supplier.id]
            profiles.append(supplier_risk_profile([{"score": ev.total_score, "date": ev.evaluation_date.isoformat(), "issues": ev.issues, "outcome": ev.outcome} for ev in supplier_evals]))
        distribution = {key: sum(p["risk_rating"] == key for p in profiles) for key in ("LOW", "MEDIUM", "HIGH", "NOT_RATED")}
        today = date.today()
        return {
            "active_suppliers": sum(s.status == "ACTIVE" for s in suppliers),
            "active_contracts": sum(c.status == "ACTIVE" for c in contracts),
            "average_performance": round(sum(e.total_score for e in evaluations) / len(evaluations), 1) if evaluations else None,
            "risk_distribution": distribution,
            "evaluations_due": sum(e.due_date and today <= e.due_date <= today + timedelta(days=30) and e.status != "APPROVED" for e in db.scalars(select(Evaluation)).all()),
            "open_evaluations": db.scalar(select(func.count()).select_from(Evaluation).where(Evaluation.status != "APPROVED")),
            "overdue_evaluations": sum(e.due_date and e.due_date < today and e.status != "APPROVED" for e in db.scalars(select(Evaluation)).all()),
            "warning_suppliers": sum(p["warning_status"] for p in profiles),
            "near_suspension": sum(p["risk_rating"] == "MEDIUM" for p in profiles),
            "suspended_suppliers": db.scalar(select(func.count(func.distinct(PerformanceDecision.supplier_id))).where(PerformanceDecision.decision_type == "SUSPENSION", PerformanceDecision.status == "APPROVED", PerformanceDecision.effective_date <= today, (PerformanceDecision.expiry_date.is_(None)) | (PerformanceDecision.expiry_date >= today))),
        }

    @app.get("/api/evaluations/{evaluation_id}/correspondence.pdf")
    def correspondence(evaluation_id: int, db: Session = Depends(get_db)):
        ev = db.get(Evaluation, evaluation_id)
        if not ev:
            raise HTTPException(404, "Evaluation not found.")
        if ev.status != "APPROVED":
            raise HTTPException(409, "Official correspondence is available only for approved evaluations.")
        supplier = ev.contract.supplier
        subjects = {
            "CONGRATULATIONS": "Contract performance evaluation — congratulations",
            "MEETS_EXPECTATIONS": "Contract performance evaluation — meets expectations",
            "WARNING": "Contract performance evaluation — warning",
            "SUSPENSION_RECOMMENDATION": "Contract performance evaluation — suspension recommendation",
        }
        model_name = "contractor" if ev.model == "CONSTRUCTION" else "consultant"
        paragraphs = [
            f"Canada has completed its {model_name} performance evaluation for the above-noted contract. The recorded score is {ev.total_score:.1f}% and the resulting classification is {ev.outcome.replace('_', ' ').title()}.",
            "The evaluation is based on the contractual performance criteria and supporting evidence retained on file. This notice does not amend the contract or create any new obligation.",
        ]
        if ev.outcome == "WARNING":
            paragraphs.append("This result requires performance improvement. Under the applicable contract terms, a further evaluation of 50% or less within two years may support a suspension decision. Any decision must follow the applicable authority, notice, representation and approval process.")
        if ev.outcome == "SUSPENSION_RECOMMENDATION":
            paragraphs.append("The system has identified this result for suspension review. This is a recommendation and not a final suspension decision until the authorized decision-maker completes procedural fairness and approval requirements.")
        content = correspondence_pdf({"date": ev.evaluation_date.isoformat(), "supplier": supplier.name, "contract": ev.contract.contract_number, "organization": ev.contract.department, "subject": subjects[ev.outcome], "paragraphs": paragraphs})
        return Response(content, media_type="application/pdf", headers={"Content-Disposition": f'attachment; filename="evaluation-{ev.id}-letter.pdf"'})

    @app.get("/api/reports/performance-history.xlsx")
    def report_xlsx(db: Session = Depends(get_db)):
        evaluations = db.scalars(select(Evaluation).where(Evaluation.status == "APPROVED").order_by(Evaluation.evaluation_date.desc())).all()
        rows = [{
            "supplier": ev.contract.supplier.name, "contract": ev.contract.contract_number,
            "standing_offer": ev.contract.standing_offer_number, "call_up": ev.contract.call_up_number,
            "project": ev.contract.project_number, "procurement_type": ev.contract.procurement_type,
            "region": ev.contract.region, "date": ev.evaluation_date.isoformat(), "evaluator": ev.evaluator,
            "model": ev.model, "score": ev.total_score, "outcome": ev.outcome, "status": ev.status,
        } for ev in evaluations]
        content = performance_history_xlsx(rows)
        return Response(content, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", headers={"Content-Disposition": 'attachment; filename="performance-history.xlsx"'})

    @app.get("/api/reports/{report_name}.pdf")
    def report_pdf(report_name: str, db: Session = Depends(get_db)):
        supported = {
            "contractor-performance-history": "Contractor Performance History",
            "consultant-performance-history": "Consultant Performance History",
            "cperf": "CPERF Report Register",
            "supplier-risk": "Supplier Risk Report",
            "suspension-eligibility": "Suspension Eligibility Report",
            "procurement-readiness": "Procurement Readiness Report",
            "trend-analysis": "Trend Analysis Report",
        }
        if report_name not in supported:
            raise HTTPException(404, "Unknown report type.")
        evaluations = db.scalars(select(Evaluation).where(Evaluation.status == "APPROVED").order_by(Evaluation.evaluation_date.desc())).all()
        suppliers = db.scalars(select(Supplier)).all()
        if report_name == "contractor-performance-history":
            items = [e for e in evaluations if e.model == "CONSTRUCTION"]
            headers = ["Supplier", "Contract", "Date", "Score", "Outcome"]
            rows = [[e.contract.supplier.name, e.contract.contract_number, e.evaluation_date.isoformat(), f"{e.total_score:.1f}%", e.outcome.replace("_", " ")] for e in items]
        elif report_name in ("consultant-performance-history", "cperf"):
            items = [e for e in evaluations if e.model in (("AE_CPERF",) if report_name == "cperf" else ("AE", "AE_CPERF"))]
            headers = ["Supplier", "Contract", "Profile", "Score", "Outcome"]
            rows = [[e.contract.supplier.name, e.contract.contract_number, e.model, f"{e.total_score:.1f}%", e.outcome.replace("_", " ")] for e in items]
        else:
            profiles = []
            for supplier in suppliers:
                items = [e for e in evaluations if e.contract.supplier_id == supplier.id]
                risk = supplier_risk_profile([{"score": e.total_score, "date": e.evaluation_date.isoformat(), "issues": e.issues, "outcome": e.outcome} for e in items])
                profiles.append((supplier, risk, items))
            if report_name == "supplier-risk":
                headers = ["Supplier", "Average", "Trend", "Risk", "Flags"]
                rows = [[s.name, r["average_score"], r["trend_score"], r["risk_rating"], ", ".join(r["flags"])] for s, r, _ in profiles]
            elif report_name == "trend-analysis":
                headers = ["Supplier", "Evaluations", "Average", "Trend", "Direction"]
                rows = [[s.name, len(items), r["average_score"], r["trend_score"], "DECLINING" if (r["trend_score"] or 0) < 0 else "STABLE/IMPROVING"] for s, r, items in profiles]
            elif report_name == "procurement-readiness":
                headers = ["Supplier", "Active contracts", "Risk", "Eligibility", "Warnings"]
                rows = [[s.name, sum(c.status == "ACTIVE" for c in s.contracts), r["risk_rating"], r["eligibility_status"], "YES" if r["warning_status"] else "NO"] for s, r, _ in profiles]
            else:
                decisions = db.scalars(select(PerformanceDecision).where(PerformanceDecision.decision_type == "SUSPENSION").order_by(PerformanceDecision.effective_date.desc())).all()
                headers = ["Supplier", "Decision", "Status", "Effective", "Expiry"]
                rows = [[db.get(Supplier, d.supplier_id).name, d.decision_type, d.status, d.effective_date.isoformat(), d.expiry_date.isoformat() if d.expiry_date else ""] for d in decisions]
                for e in evaluations:
                    if e.outcome == "SUSPENSION_RECOMMENDATION":
                        rows.append([e.contract.supplier.name, "RECOMMENDATION", "REVIEW REQUIRED", e.evaluation_date.isoformat(), ""])
        content = tabular_report_pdf(supported[report_name], f"Generated from approved supplier performance records · {date.today().isoformat()}", headers, rows)
        return Response(content, media_type="application/pdf", headers={"Content-Disposition": f'attachment; filename="{report_name}.pdf"'})

    @app.get("/", response_class=HTMLResponse)
    def ui_dashboard(request: Request, db: Session = Depends(get_db)):
        data = dashboard(db)
        recent = db.scalars(select(Evaluation).order_by(Evaluation.evaluation_date.desc()).limit(8)).all()
        return TEMPLATES.TemplateResponse(request, "dashboard.html", {"dashboard": data, "evaluations": recent})

    @app.get("/suppliers", response_class=HTMLResponse)
    def ui_suppliers(request: Request, db: Session = Depends(get_db)):
        suppliers = db.scalars(select(Supplier).options(selectinload(Supplier.contracts).selectinload(Contract.evaluations)).order_by(Supplier.name)).all()
        rows = []
        for item in suppliers:
            approved = [ev for c in item.contracts for ev in c.evaluations if ev.status == "APPROVED"]
            rows.append({"supplier": item, "contract_summary": contract_summary(item.contracts), "risk": supplier_risk_profile([{"score": ev.total_score, "date": ev.evaluation_date.isoformat(), "issues": ev.issues, "outcome": ev.outcome} for ev in approved])})
        return TEMPLATES.TemplateResponse(request, "suppliers.html", {"rows": rows})

    @app.get("/evaluations", response_class=HTMLResponse)
    def ui_evaluations(request: Request, db: Session = Depends(get_db)):
        evaluations = db.scalars(select(Evaluation).order_by(Evaluation.evaluation_date.desc())).all()
        return TEMPLATES.TemplateResponse(request, "evaluations.html", {"evaluations": evaluations})

    @app.get("/evaluations/new", response_class=HTMLResponse)
    def ui_new_evaluation(request: Request, user: str = Depends(require_roles("EVALUATOR")), db: Session = Depends(get_db)):
        contracts = db.scalars(select(Contract).order_by(Contract.contract_number)).all()
        return TEMPLATES.TemplateResponse(request, "evaluation_form.html", {
            "contracts": contracts,
            "suppliers": db.scalars(select(Supplier).order_by(Supplier.name)).all(),
            "construction": CONSTRUCTION_CRITERIA,
            "construction_optional": CONSTRUCTION_OPTIONAL_CRITERIA,
            "ae": AE_DEFAULT_WEIGHTS,
            "ae_cperf": AE_CPERF_WEIGHTS,
            "ae_cperf_optional": AE_CPERF_OPTIONAL_CRITERIA,
        })

    @app.get("/evaluations/{evaluation_id}/view", response_class=HTMLResponse)
    def ui_evaluation_detail(evaluation_id: int, request: Request, db: Session = Depends(get_db)):
        ev = db.get(Evaluation, evaluation_id)
        if not ev:
            raise HTTPException(404, "Evaluation not found.")
        history = evaluation_history(evaluation_id, db)
        roles = set(request.state.principal["roles"])
        allowed_actions = available_workflow_actions(ev.status, roles)
        score_summary = evaluation_dict(ev)
        can_edit = ev.status == "RETURNED" and is_assigned_evaluator(ev, request.state.principal, db)
        return TEMPLATES.TemplateResponse(request, "evaluation_detail.html", {"evaluation": ev, "score_summary": score_summary, "history": history, "allowed_actions": allowed_actions, "can_edit": can_edit})

    @app.get("/evaluations/{evaluation_id}/edit", response_class=HTMLResponse)
    def ui_edit_evaluation(evaluation_id: int, request: Request, user: str = Depends(require_roles("EVALUATOR")), db: Session = Depends(get_db)):
        ev = db.get(Evaluation, evaluation_id)
        if not ev:
            raise HTTPException(404, "Evaluation not found.")
        if ev.status not in {"DRAFT", "RETURNED"}:
            raise HTTPException(409, "Only draft or returned evaluations may be edited.")
        require_assigned_evaluator(ev, request, db)
        history = evaluation_history(evaluation_id, db)
        return TEMPLATES.TemplateResponse(request, "evaluation_form.html", {
            "contracts": [],
            "construction": CONSTRUCTION_CRITERIA,
            "construction_optional": CONSTRUCTION_OPTIONAL_CRITERIA,
            "ae": AE_DEFAULT_WEIGHTS,
            "ae_cperf": AE_CPERF_WEIGHTS,
            "ae_cperf_optional": AE_CPERF_OPTIONAL_CRITERIA,
            "evaluation": ev,
            "return_comments": history["return_comments"],
        })

    return app


def seed_demo(db: Session) -> None:
    if db.scalar(select(func.count()).select_from(Supplier)):
        return
    suppliers = [
        Supplier(name="Atlantic Marine Constructors Ltd.", business_number="DEMO-AC-1001"),
        Supplier(name="Northstar Engineering Group", business_number="DEMO-NE-1002"),
        Supplier(name="Boreal Environmental Services Inc.", business_number="DEMO-BE-1003"),
        Supplier(name="Harbourline Architecture JV", business_number="DEMO-HA-1004"),
    ]
    db.add_all(suppliers); db.flush()
    contracts = [
        Contract(supplier_id=suppliers[0].id, contract_number="F5211-250101", project_number="MAR-24-018", procurement_type="Construction", region="Atlantic", department="Example Contracting Organization", performance_regime="GI16_GC1_22", status="ACTIVE"),
        Contract(supplier_id=suppliers[1].id, contract_number="F5211-250202", standing_offer_number="SO-ENG-2025-04", call_up_number="CU-017", project_number="PAC-25-004", procurement_type="Standing Offer Call-Ups", region="Pacific", department="Example Contracting Organization", performance_regime="AE_EXTENDED", status="ACTIVE"),
        Contract(supplier_id=suppliers[2].id, contract_number="F5211-240303", project_number="C&A-23-077", procurement_type="Environmental Services", region="Central and Arctic", department="Example Contracting Organization", performance_regime="GI23_GC26_2913_1", status="CLOSED"),
        Contract(supplier_id=suppliers[3].id, contract_number="F5211-250404", standing_offer_number="SO-AE-2025-02", project_number="QUE-25-021", procurement_type="Architectural Services", region="Quebec", department="Example Contracting Organization", performance_regime="AE_EXTENDED", status="ACTIVE"),
    ]
    db.add_all(contracts); db.flush()
    demo = [
        (contracts[0], "CONSTRUCTION", {name: score for name, score in zip(CONSTRUCTION_CRITERIA, [18, 16, 17, 15, 19])}, None, ["schedule"], "APPROVED", date.today() - timedelta(days=45)),
        (contracts[1], "AE", {name: score for name, score in zip(AE_DEFAULT_WEIGHTS, [14, 13, 15, 12, 13, 9, 11, 14])}, AE_DEFAULT_WEIGHTS, ["schedule"], "APPROVED", date.today() - timedelta(days=30)),
        (contracts[2], "AE_CPERF", {name: score for name, score in zip(AE_CPERF_WEIGHTS, [9, 10, 8, 7, 9])}, AE_CPERF_WEIGHTS, ["cost control", "contract administration"], "APPROVED", date.today() - timedelta(days=400)),
        (contracts[2], "AE_CPERF", {name: score for name, score in zip(AE_CPERF_WEIGHTS, [8, 9, 7, 6, 8])}, AE_CPERF_WEIGHTS, ["cost control", "contract administration"], "APPROVED", date.today() - timedelta(days=80)),
        (contracts[3], "AE", {name: 15 for name in AE_DEFAULT_WEIGHTS}, AE_DEFAULT_WEIGHTS, [], "SUBMITTED", date.today() - timedelta(days=5)),
    ]
    for contract, model, scores, weights, issues, status, eval_date in demo:
        result, weights = calculate(model, scores, weights)
        ev = Evaluation(contract_id=contract.id, model=model, evaluation_date=eval_date, due_date=eval_date + timedelta(days=30), evaluator="Project Authority", scores=scores, weights=weights, ratings=result["ratings"], issues=issues, comments="Demonstration record with supporting evidence retained on file.", total_score=result["percentage"], outcome=result["outcome"], status=status, version=1)
        db.add(ev); db.flush()
        db.add(EvaluationVersion(evaluation_id=ev.id, version=1, snapshot=snapshot(ev), user="Seed Administrator"))
        db.add(AuditEntry(evaluation_id=ev.id, user="Seed Administrator", action="CREATE", revised_value=json.dumps(snapshot(ev), default=str)))
    db.commit()
