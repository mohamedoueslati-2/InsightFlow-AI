"""
Pydantic Schemas for AI Cleaning Agent structured outputs and API responses.
"""

from typing import List, Optional, Any, Dict, Literal
from pydantic import BaseModel, Field, model_validator

ApprovalMode = Literal["auto_safe", "review_all", "full_auto"]


class CleaningPlanItem(BaseModel):
    id: str
    intent: Literal["transform", "recommend", "flag"] = "transform"
    problem_id: str
    scope: Literal["column", "dataset"] = "column"
    columns: List[str] = Field(default_factory=list)
    diagnosis: str
    strategy: str
    benefit: str = ""
    tradeoff: str = ""
    recommendation_rank: int = Field(default=0, ge=0, le=2)
    recommendation_reason: str = ""
    expected_effect: str
    risk_hint: Literal["low", "medium", "high"] = "medium"
    requires_human_review: bool = True
    allow_row_deletion: bool = False
    allow_column_deletion: bool = False
    allow_new_missing: bool = False
    status: str = "planned"


class CleaningPlan(BaseModel):
    plan_id: str
    dataset_understanding: str
    items: List[CleaningPlanItem] = Field(default_factory=list, max_length=30)
    reply: str = ""


class GeneratedProgram(BaseModel):
    code: str = Field(max_length=20000)
    explanation: str = ""


class DatasetDelta(BaseModel):
    rows_before: int = 0
    rows_after: int = 0
    columns_before: int = 0
    columns_after: int = 0
    columns_added: List[str] = Field(default_factory=list)
    columns_removed: List[str] = Field(default_factory=list)
    dtypes_before: Dict[str, str] = Field(default_factory=dict)
    dtypes_after: Dict[str, str] = Field(default_factory=dict)
    missing_before: Dict[str, int] = Field(default_factory=dict)
    missing_after: Dict[str, int] = Field(default_factory=dict)
    new_missing_values: int = 0
    duplicates_before: int = 0
    duplicates_after: int = 0
    unique_before: Dict[str, int] = Field(default_factory=dict)
    unique_after: Dict[str, int] = Field(default_factory=dict)
    changed_cells: int = 0
    changed_rows: int = 0
    changed_ratio: float = 0
    distribution_changes: Dict[str, Any] = Field(default_factory=dict)
    date_ranges: Dict[str, Any] = Field(default_factory=dict)
    samples: List[Dict[str, Any]] = Field(default_factory=list)


class ValidationReport(DatasetDelta):
    passed: bool = False
    target_issue_improved: Optional[bool] = None
    warnings: List[str] = Field(default_factory=list)
    errors: List[str] = Field(default_factory=list)
    schema_changes: List[str] = Field(default_factory=list)
    representation_only: bool = False
    exact_duplicate_removal: bool = False


class RiskAssessment(BaseModel):
    level: Literal["low", "medium", "high"] = "high"
    score: int = Field(default=100, ge=0, le=100)
    reasons: List[str] = Field(default_factory=list)
    requires_human_review: bool = True


class ExecutionAttempt(BaseModel):
    plan_item_id: str
    attempt: int
    code_hash: str = ""
    stage: str
    error: str = ""
    execution: Dict[str, Any] = Field(default_factory=dict)
    validation: Dict[str, Any] = Field(default_factory=dict)


class CleaningProposal(BaseModel):
    id: str
    plan_item_id: str
    problem_id: str
    title: str
    explanation: str
    generated_code: str
    code_hash: str
    risk: RiskAssessment
    validation: ValidationReport
    rows_affected: int
    rows_before: int
    rows_after: int
    changed_cells: int
    samples: List[Dict[str, Any]] = Field(default_factory=list)
    fingerprint: str
    candidate_fingerprint: str
    plan_item: CleaningPlanItem
    attempt_count: int = 1
    status: Literal["ready", "applied", "dismissed", "stale", "failed"] = "ready"


class CleaningRequest(BaseModel):
    approval_mode: ApprovalMode = "auto_safe"


class ValidationResult(BaseModel):
    """Deterministic, tool-measured before/after comparison for a single mutation attempt."""
    passed: bool = Field(..., description="Whether the deterministic validator accepted this change")
    metric: str = Field(..., description="Name of the metric measured (e.g. 'duplicate_rows', 'unique_category_count')")
    before: Any = Field(default=None, description="Metric value measured before the operation")
    after: Any = Field(default=None, description="Metric value measured after the operation")
    details: str = Field(default="", description="Human-readable explanation of the measurement and outcome")


class CleaningAction(BaseModel):
    """A single accepted (kept) cleaning action, backed by deterministic validation."""
    problem: str = Field(default="unspecified", description="The problem this action addressed")
    column: Optional[str] = Field(default=None, description="Target column, null for dataset-wide operations")
    action: str = Field(default="unspecified", description="Name of the tool/operation applied")
    reason: str = Field(default="", description="Why this operation was chosen for this problem")
    rows_affected: int = Field(default=0, description="Number of rows touched by this action")
    confidence: float = Field(default=0.5, ge=0.0, le=1.0, description="Agent's confidence signal (not the acceptance criterion)")
    validation: Dict[str, Any] = Field(default_factory=dict, description="Deterministic tool-measured before/after validation result")
    resolved_by: str = Field(default="agent", description="'agent' (automatic) or 'user' (manual human-in-the-loop decision)")
    plan_item_id: Optional[str] = None
    generated_code: Optional[str] = None
    code_hash: Optional[str] = None
    risk_level: Optional[str] = None
    attempt_count: int = 0
    execution: Dict[str, Any] = Field(default_factory=dict)


class RemainingIssue(BaseModel):
    """A detected problem that was not automatically resolved."""
    id: str = Field(default="", description="Stable identifier for this issue, used to address it in a resolution conversation")
    scope: str = Field(default="column", description="Problem scope: 'column' or 'dataset'")
    column: Optional[str] = Field(default=None, description="Column name for column-level issues, null for dataset-level issues")
    type: str = Field(default="issue", description="Problem category")
    severity: str = Field(default="medium", description="Severity level: 'high', 'medium', or 'low'")
    evidence: str = Field(default="", description="Factual evidence backing the issue")
    confidence: float = Field(default=0.8, ge=0.0, le=1.0, description="Confidence in this problem identification")
    reason_not_fixed: str = Field(default="", description="Why this was not automatically corrected (no safe tool, ambiguous, budget exhausted, etc.)")


class ConversationTurn(BaseModel):
    """One turn in a per-issue resolution conversation with the agent."""
    role: str = Field(..., description="'user' or 'agent'")
    text: str = Field(default="", description="Message text")
    execution_status: Optional[str] = Field(default=None, description="Tool-verified 'applied' or 'no_change'; absent on legacy turns")


class CleaningStep(BaseModel):
    """One entry in the actual sequence of tool calls/decisions taken during the run (non-reproducible trace). Always recomputed from the toolkit's own log downstream."""
    step: int = Field(default=0, description="Sequence order of this step")
    tool: str = Field(default="unspecified", description="Tool/operation name invoked")
    column: Optional[str] = Field(default=None, description="Target column, if any")
    outcome: str = Field(default="", description="'kept' | 'rolled_back' | 'no_correction_applicable' | 'budget_exhausted' | 'error' | 'inspection'")
    detail: str = Field(default="", description="Short human-readable outcome detail")


class CleaningReport(BaseModel):
    """Complete structured cleaning report produced by the Cleaning Agent."""
    status: str = Field(default="partially_cleaned", description="'cleaned' | 'partially_cleaned' | 'unchanged' | 'reviewed' (decisions completed with accepted findings; recomputed downstream)")
    summary: str = Field(default="", description="Placeholder only -- always recomputed deterministically downstream, never trusted from the LLM")
    changes_applied: int = Field(default=0, description="Total number of kept actions")
    rows_affected: int = Field(default=0, description="Total distinct rows touched across all kept actions")
    actions: List[CleaningAction] = Field(default_factory=list, description="Accepted, validated cleaning actions")
    remaining_issues: List[RemainingIssue] = Field(default_factory=list, description="Detected problems not automatically resolved")
    validation: Dict[str, Any] = Field(default_factory=dict, description="Overall before/after dataset-level validation summary")
    quality_before: float = Field(default=0.0, ge=0.0, le=100.0, description="Deterministic quality score before cleaning")
    quality_after: float = Field(default=0.0, ge=0.0, le=100.0, description="Deterministic quality score after cleaning")
    steps: List[CleaningStep] = Field(default_factory=list, description="Actual sequence of tool calls/decisions taken during this run")
    issue_conversations: Dict[str, List[ConversationTurn]] = Field(
        default_factory=dict, description="Per-issue resolution chat threads, keyed by RemainingIssue.id"
    )
    issue_proposals: Dict[str, List[Dict[str, Any]]] = Field(default_factory=dict, description="Saved previews and their execution status, per issue")
    accepted_issues: List[Dict[str, Any]] = Field(default_factory=list, description="Findings explicitly accepted unchanged by the user; retained in quality scoring")
    run_id: Optional[str] = None
    approval_mode: ApprovalMode = "auto_safe"
    plan: Optional[CleaningPlan] = None
    code_attempts: List[ExecutionAttempt] = Field(default_factory=list)
    generated_programs: List[Dict[str, Any]] = Field(default_factory=list)
    sandbox: Dict[str, Any] = Field(default_factory=dict)
    risk_summary: Dict[str, Any] = Field(default_factory=dict)


class IssueMessageRequest(BaseModel):
    """One user message sent into a specific remaining issue's resolution conversation with the agent."""
    issue_id: str = Field(..., min_length=1, max_length=512, description="RemainingIssue.id this message is addressed to")
    message: str = Field(default="", max_length=4000, description="The user's free-text message/proposal")
    proposal_id: Optional[str] = Field(default=None, min_length=1, max_length=64)
    decision: Optional[Literal["apply", "dismiss", "keep", "reopen"]] = None

    @model_validator(mode="after")
    def validate_decision(self):
        if self.decision in {"keep", "reopen"}:
            if self.proposal_id is not None:
                raise ValueError("Une décision de revue ne cible pas une proposition.")
        elif (self.proposal_id is None) != (self.decision is None):
            raise ValueError("proposal_id et decision doivent être fournis ensemble.")
        return self


class CleaningListItem(BaseModel):
    """Metadata of an existing saved cleaning report."""
    stem: str
    file_type: str
    filename: str
    report_file: str
    created_at: str
    changes_applied: int
    rows_affected: int
    remaining_issues: int
    status: str
