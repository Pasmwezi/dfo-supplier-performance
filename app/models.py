from __future__ import annotations

from datetime import date, datetime, timezone

from sqlalchemy import Date, DateTime, Float, ForeignKey, Integer, JSON, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow():
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class UserAccount(Base):
    __tablename__ = "user_accounts"
    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    display_name: Mapped[str] = mapped_column(String(200))
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(30), index=True)
    active: Mapped[bool] = mapped_column(default=True, index=True)
    must_change_password: Mapped[bool] = mapped_column(default=True)
    created_by: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    password_changed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    sessions: Mapped[list["UserSession"]] = relationship(cascade="all, delete-orphan")


class UserSession(Base):
    __tablename__ = "user_sessions"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("user_accounts.id"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AccessAudit(Base):
    __tablename__ = "access_audit"
    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(80), index=True)
    action: Mapped[str] = mapped_column(String(80), index=True)
    actor: Mapped[str | None] = mapped_column(String(200), nullable=True)
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Supplier(Base):
    __tablename__ = "suppliers"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(250), unique=True, index=True)
    business_number: Mapped[str | None] = mapped_column(String(80), nullable=True)
    status: Mapped[str] = mapped_column(String(30), default="ACTIVE")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    contracts: Mapped[list["Contract"]] = relationship(back_populates="supplier", cascade="all, delete-orphan")


class Contract(Base):
    __tablename__ = "contracts"
    id: Mapped[int] = mapped_column(primary_key=True)
    supplier_id: Mapped[int] = mapped_column(ForeignKey("suppliers.id"), index=True)
    contract_number: Mapped[str] = mapped_column(String(100), unique=True, index=True)
    standing_offer_number: Mapped[str | None] = mapped_column(String(100), nullable=True)
    call_up_number: Mapped[str | None] = mapped_column(String(100), nullable=True)
    project_number: Mapped[str | None] = mapped_column(String(100), nullable=True)
    procurement_type: Mapped[str] = mapped_column(String(100), index=True)
    region: Mapped[str] = mapped_column(String(100), index=True)
    department: Mapped[str] = mapped_column(String(200), default="Fisheries and Oceans Canada")
    contract_value: Mapped[float | None] = mapped_column(Float, nullable=True)
    performance_evaluation_required: Mapped[bool] = mapped_column(default=True)
    performance_regime: Mapped[str] = mapped_column(String(80), default="APPLICABLE_CONTRACT_TERMS")
    status: Mapped[str] = mapped_column(String(30), default="ACTIVE", index=True)
    start_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    end_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    supplier: Mapped[Supplier] = relationship(back_populates="contracts")
    evaluations: Mapped[list["Evaluation"]] = relationship(back_populates="contract", cascade="all, delete-orphan")


class Evaluation(Base):
    __tablename__ = "evaluations"
    id: Mapped[int] = mapped_column(primary_key=True)
    contract_id: Mapped[int] = mapped_column(ForeignKey("contracts.id"), index=True)
    model: Mapped[str] = mapped_column(String(30), index=True)
    evaluation_date: Mapped[date] = mapped_column(Date, default=date.today)
    due_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    evaluator: Mapped[str] = mapped_column(String(200))
    scores: Mapped[dict] = mapped_column(JSON)
    weights: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    ratings: Mapped[dict | list] = mapped_column(JSON)
    issues: Mapped[list] = mapped_column(JSON, default=list)
    comments: Mapped[str] = mapped_column(Text, default="")
    total_score: Mapped[float] = mapped_column(Float)
    outcome: Mapped[str] = mapped_column(String(60), index=True)
    status: Mapped[str] = mapped_column(String(30), default="DRAFT", index=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    contract: Mapped[Contract] = relationship(back_populates="evaluations")
    versions: Mapped[list["EvaluationVersion"]] = relationship(cascade="all, delete-orphan")
    audit_entries: Mapped[list["AuditEntry"]] = relationship(cascade="all, delete-orphan")
    attachments: Mapped[list["Attachment"]] = relationship(cascade="all, delete-orphan")
    project_details: Mapped["EvaluationProjectDetails"] = relationship(back_populates="evaluation", uselist=False, cascade="all, delete-orphan")


class EvaluationProjectDetails(Base):
    __tablename__ = "evaluation_project_details"
    evaluation_id: Mapped[int] = mapped_column(ForeignKey("evaluations.id"), primary_key=True)
    client_reference_number: Mapped[str | None] = mapped_column(String(100), nullable=True)
    description_of_work: Mapped[str | None] = mapped_column(Text, nullable=True)
    firm_address: Mapped[str | None] = mapped_column(Text, nullable=True)
    contractor_superintendent: Mapped[str | None] = mapped_column(String(200), nullable=True)
    project_manager_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    project_manager_telephone: Mapped[str | None] = mapped_column(String(50), nullable=True)
    project_manager_fax: Mapped[str | None] = mapped_column(String(50), nullable=True)
    project_manager_cell: Mapped[str | None] = mapped_column(String(50), nullable=True)
    project_manager_email: Mapped[str | None] = mapped_column(String(254), nullable=True)
    contract_award_amount: Mapped[float | None] = mapped_column(Float, nullable=True)
    contract_award_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    final_amount: Mapped[float | None] = mapped_column(Float, nullable=True)
    contract_completion_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    contract_changes_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    final_certificate_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    evaluation: Mapped[Evaluation] = relationship(back_populates="project_details")


class EvaluationVersion(Base):
    __tablename__ = "evaluation_versions"
    id: Mapped[int] = mapped_column(primary_key=True)
    evaluation_id: Mapped[int] = mapped_column(ForeignKey("evaluations.id"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    snapshot: Mapped[dict] = mapped_column(JSON)
    user: Mapped[str] = mapped_column(String(200))
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AuditEntry(Base):
    __tablename__ = "audit_entries"
    id: Mapped[int] = mapped_column(primary_key=True)
    evaluation_id: Mapped[int] = mapped_column(ForeignKey("evaluations.id"), index=True)
    user: Mapped[str] = mapped_column(String(200))
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    action: Mapped[str] = mapped_column(String(80))
    field_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    original_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    revised_value: Mapped[str | None] = mapped_column(Text, nullable=True)


class PerformanceDecision(Base):
    __tablename__ = "performance_decisions"
    id: Mapped[int] = mapped_column(primary_key=True)
    supplier_id: Mapped[int] = mapped_column(ForeignKey("suppliers.id"), index=True)
    evaluation_id: Mapped[int | None] = mapped_column(ForeignKey("evaluations.id"), nullable=True, index=True)
    decision_type: Mapped[str] = mapped_column(String(40), index=True)
    status: Mapped[str] = mapped_column(String(30), index=True)
    effective_date: Mapped[date] = mapped_column(Date)
    expiry_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    rationale: Mapped[str] = mapped_column(Text)
    notice_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    representation_deadline: Mapped[date | None] = mapped_column(Date, nullable=True)
    representations_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    legal_review_reference: Mapped[str | None] = mapped_column(String(200), nullable=True)
    delegated_authority_reference: Mapped[str | None] = mapped_column(String(200), nullable=True)
    authority: Mapped[str] = mapped_column(String(200))
    recorded_by: Mapped[str] = mapped_column(String(200))
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class DecisionAudit(Base):
    __tablename__ = "decision_audit"
    id: Mapped[int] = mapped_column(primary_key=True)
    decision_id: Mapped[int] = mapped_column(ForeignKey("performance_decisions.id"), index=True)
    user: Mapped[str] = mapped_column(String(200))
    action: Mapped[str] = mapped_column(String(80))
    original_status: Mapped[str | None] = mapped_column(String(30), nullable=True)
    revised_status: Mapped[str] = mapped_column(String(30))
    snapshot: Mapped[dict] = mapped_column(JSON)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Attachment(Base):
    __tablename__ = "attachments"
    id: Mapped[int] = mapped_column(primary_key=True)
    evaluation_id: Mapped[int] = mapped_column(ForeignKey("evaluations.id"), index=True)
    original_filename: Mapped[str] = mapped_column(String(255))
    stored_filename: Mapped[str] = mapped_column(String(255))
    content_type: Mapped[str | None] = mapped_column(String(120), nullable=True)
    sha256: Mapped[str] = mapped_column(String(64))
    uploaded_by: Mapped[str] = mapped_column(String(200))
    uploaded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
