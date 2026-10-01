from datetime import date
from typing import Literal

from pydantic import BaseModel, Field, model_validator

ProcurementType = Literal["Construction", "Architectural Services", "Engineering Services", "Environmental Services", "Feasibility Studies", "Investigations", "Contract Administration Services", "Standing Offer Call-Ups", "Supply Arrangement Contracts", "A&E Standing Offer", "Construction Standing Offer", "Construction Call-Up"]
Region = Literal["Atlantic", "Quebec", "Ontario and Prairie", "Pacific", "Central and Arctic", "National Capital", "National"]
Issue = Literal["schedule", "cost control", "safety", "contract administration"]
UserRole = Literal["EVALUATOR", "REVIEWER", "APPROVER", "DECISION_MAKER"]


class UserCreate(BaseModel):
    username: str = Field(min_length=3, max_length=80, pattern=r"^[a-z0-9._-]+$")
    display_name: str = Field(min_length=2, max_length=200)
    role: UserRole
    temporary_password: str = Field(min_length=12, max_length=128)


class UserStatusUpdate(BaseModel):
    active: bool


class WorkflowActionRequest(BaseModel):
    comment: str | None = Field(default=None, max_length=4000)


class SupplierCreate(BaseModel):
    name: str = Field(min_length=2, max_length=250)
    business_number: str | None = Field(default=None, max_length=80)


class ContractCreate(BaseModel):
    supplier_id: int
    contract_number: str = Field(min_length=2, max_length=100)
    standing_offer_number: str | None = Field(default=None, max_length=100)
    call_up_number: str | None = Field(default=None, max_length=100)
    project_number: str | None = Field(default=None, max_length=100)
    procurement_type: ProcurementType
    region: Region
    department: str = Field(default="Contracting Organization", max_length=200)
    contract_value: float | None = Field(default=None, ge=0)
    performance_evaluation_required: bool = True
    performance_regime: Literal["GI16_GC1_22", "GI23_GC26_2913_1", "AE_EXTENDED", "DFO_AE_EXTENDED", "APPLICABLE_CONTRACT_TERMS"] = "APPLICABLE_CONTRACT_TERMS"
    status: Literal["ACTIVE", "CLOSED", "SUSPENDED"] = "ACTIVE"
    start_date: date | None = None
    end_date: date | None = None

    @model_validator(mode="after")
    def dates_in_order(self):
        if self.start_date and self.end_date and self.end_date < self.start_date:
            raise ValueError("Contract end date cannot precede start date.")
        return self


class InlineContractCreate(BaseModel):
    contract_number: str = Field(min_length=2, max_length=100)
    standing_offer_number: str | None = Field(default=None, max_length=100)
    call_up_number: str | None = Field(default=None, max_length=100)
    project_number: str | None = Field(default=None, max_length=100)
    procurement_type: ProcurementType
    region: Region
    department: str = Field(default="Contracting Organization", max_length=200)
    contract_value: float | None = Field(default=None, ge=0)
    performance_evaluation_required: bool = True
    performance_regime: Literal["GI16_GC1_22", "GI23_GC26_2913_1", "AE_EXTENDED", "DFO_AE_EXTENDED"]
    status: Literal["ACTIVE", "CLOSED", "SUSPENDED"] = "ACTIVE"
    start_date: date | None = None
    end_date: date | None = None

    @model_validator(mode="after")
    def dates_in_order(self):
        if self.start_date and self.end_date and self.end_date < self.start_date:
            raise ValueError("Contract end date cannot precede start date.")
        return self


class ProjectDetails(BaseModel):
    client_reference_number: str | None = Field(default=None, max_length=100)
    description_of_work: str | None = Field(default=None, max_length=10000)
    firm_address: str | None = Field(default=None, max_length=2000)
    contractor_superintendent: str | None = Field(default=None, max_length=200)
    project_manager_name: str | None = Field(default=None, max_length=200)
    project_manager_telephone: str | None = Field(default=None, max_length=50)
    project_manager_fax: str | None = Field(default=None, max_length=50)
    project_manager_cell: str | None = Field(default=None, max_length=50)
    project_manager_email: str | None = Field(default=None, max_length=254)
    contract_award_amount: float | None = Field(default=None, ge=0)
    contract_award_date: date | None = None
    final_amount: float | None = Field(default=None, ge=0)
    contract_completion_date: date | None = None
    contract_changes_count: int | None = Field(default=None, ge=0)
    final_certificate_date: date | None = None

    @model_validator(mode="after")
    def project_dates_in_order(self):
        if self.contract_award_date and self.contract_completion_date and self.contract_completion_date < self.contract_award_date:
            raise ValueError("Contract completion date cannot precede the contract award date.")
        if self.contract_award_date and self.final_certificate_date and self.final_certificate_date < self.contract_award_date:
            raise ValueError("Final certificate date cannot precede the contract award date.")
        if self.project_manager_email and ("@" not in self.project_manager_email or "." not in self.project_manager_email.rsplit("@", 1)[-1]):
            raise ValueError("Project manager email address is invalid.")
        return self


class EvaluationCreate(BaseModel):
    contract_id: int | None = None
    supplier_id: int | None = Field(default=None, gt=0)
    new_supplier: SupplierCreate | None = None
    new_contract: InlineContractCreate | None = None
    model: Literal["CONSTRUCTION", "AE", "AE_CPERF"]
    evaluation_date: date = Field(default_factory=date.today)
    due_date: date | None = None
    evaluator: str = Field(min_length=2, max_length=200)
    scores: dict[str, float | None]
    weights: dict[str, float] | None = None
    comments: str = Field(default="", max_length=20000)
    issues: list[Issue] = Field(default_factory=list)
    project_details: ProjectDetails | None = None

    @model_validator(mode="after")
    def dates_in_order(self):
        if self.due_date and self.due_date < self.evaluation_date:
            raise ValueError("Evaluation due date cannot precede evaluation date.")
        existing = self.contract_id is not None
        has_supplier = self.supplier_id is not None
        has_new_supplier = self.new_supplier is not None
        has_new_contract = self.new_contract is not None
        valid_existing = existing and not (has_supplier or has_new_supplier or has_new_contract)
        valid_inline = not existing and has_new_contract and (has_supplier != has_new_supplier)
        if not (valid_existing or valid_inline):
            raise ValueError("Provide exactly one identification mode: contract_id alone, supplier_id with new_contract, or new_supplier with new_contract.")
        return self


class DecisionCreate(BaseModel):
    supplier_id: int
    evaluation_id: int | None = None
    decision_type: Literal["WARNING", "SUSPENSION", "ELIGIBILITY_OVERRIDE"]
    status: Literal["PENDING", "APPROVED", "DECLINED", "REVOKED"]
    effective_date: date
    expiry_date: date | None = None
    rationale: str = Field(min_length=10)
    authority: str = Field(min_length=2)
    notice_date: date | None = None
    representation_deadline: date | None = None
    representations_summary: str | None = Field(default=None, max_length=10000)
    legal_review_reference: str | None = Field(default=None, max_length=200)
    delegated_authority_reference: str | None = Field(default=None, max_length=200)

    @model_validator(mode="after")
    def procedural_dates_in_order(self):
        if self.expiry_date and self.expiry_date < self.effective_date:
            raise ValueError("Decision expiry date cannot precede its effective date.")
        if self.notice_date and self.representation_deadline and self.notice_date > self.representation_deadline:
            raise ValueError("Notice date cannot follow the representation deadline.")
        if self.representation_deadline and self.representation_deadline >= self.effective_date:
            raise ValueError("The representation deadline must precede the decision effective date.")
        return self


class EvaluationUpdate(BaseModel):
    evaluation_date: date | None = None
    due_date: date | None = None
    evaluator: str | None = Field(default=None, min_length=2, max_length=200)
    scores: dict[str, float | None] | None = None
    weights: dict[str, float] | None = None
    comments: str | None = Field(default=None, max_length=20000)
    issues: list[Issue] | None = None
    project_details: ProjectDetails | None = None

    @model_validator(mode="after")
    def required_fields_cannot_be_null(self):
        required = {"evaluation_date", "evaluator", "scores", "comments", "issues", "project_details"}
        null_fields = sorted(field for field in required & self.model_fields_set if getattr(self, field) is None)
        if null_fields:
            raise ValueError(f"These evaluation fields cannot be null: {', '.join(null_fields)}.")
        return self
