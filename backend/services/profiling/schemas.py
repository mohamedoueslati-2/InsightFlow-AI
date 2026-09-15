"""
Pydantic Schemas for AI Profiling Agent structured outputs and API responses.
"""

from typing import List, Optional, Any, Dict
from pydantic import BaseModel, Field


class DatasetMetrics(BaseModel):
    """Overall technical metrics of the dataset."""
    rows: int = Field(..., description="Total number of rows in the dataset")
    columns: int = Field(..., description="Total number of columns in the dataset")
    duplicate_rows: int = Field(default=0, description="Total number of exact duplicate rows")
    missing_values: int = Field(default=0, description="Total missing/null cell count across the dataset")
    memory_usage_kb: Optional[float] = Field(default=None, description="Memory usage in kilobytes")


class Understanding(BaseModel):
    """High-level semantic interpretation of the dataset."""
    description: str = Field(..., description="High-level description of what the dataset appears to represent")
    confidence: float = Field(default=0.5, ge=0.0, le=1.0, description="Confidence score between 0.0 and 1.0")


class ColumnProfile(BaseModel):
    """Profile of an individual column."""
    name: str = Field(..., description="Column name")
    observed_type: str = Field(..., description="Observed Python/Pandas data type")
    likely_role: str = Field(..., description="Inferred semantic role (e.g. boolean, email, phone, ip_address, url, timestamp, identifier, ordinal, categorical, count, percentage/ratio, monetary, measure, text, unknown)")
    confidence: float = Field(default=0.5, ge=0.0, le=1.0, description="Confidence score between 0.0 and 1.0")
    observations: List[str] = Field(default_factory=list, description="Key objective and interpretive observations for this column")


class QualityProblem(BaseModel):
    """Detected potential data quality problem."""
    scope: str = Field(default="column", description="Problem scope: 'column' or 'dataset'")
    column: Optional[str] = Field(default=None, description="Column name for column-level issues, null for dataset-level issues")
    type: str = Field(..., description="Problem category (e.g., suspicious_values, inconsistent_formats, high_missingness, duplicate_records, outliers)")
    severity: str = Field(default="medium", description="Severity level: 'high', 'medium', or 'low'")
    evidence: str = Field(..., description="Factual evidence backing the issue")
    confidence: float = Field(default=0.8, ge=0.0, le=1.0, description="Confidence in this problem identification")


class Investigation(BaseModel):
    """In-depth investigation of suspicious patterns, distributions, or anomalies."""
    column: str = Field(..., description="Column or relationship investigated")
    observation: str = Field(..., description="Summary of the investigation findings")
    examples: List[str] = Field(default_factory=list, description="Concrete examples or evidence observed")


class QualitySummary(BaseModel):
    """Overall summary of data quality."""
    missing_values: int = Field(default=0, description="Total missing values count")
    duplicate_rows: int = Field(default=0, description="Total duplicate rows count")
    potential_issues: int = Field(default=0, description="Total count of detected potential issues")
    quality_score: Optional[float] = Field(default=None, ge=0.0, le=100.0, description="Overall estimated quality score out of 100")


class ProfilingReport(BaseModel):
    """Complete structured profiling report produced by the Profiling Agent."""
    dataset: DatasetMetrics = Field(..., description="Dataset summary metrics")
    understanding: Understanding = Field(..., description="High-level understanding and confidence")
    columns: List[ColumnProfile] = Field(default_factory=list, description="Detailed profile for each column")
    problems: List[QualityProblem] = Field(default_factory=list, description="List of detected data quality problems")
    investigations: List[Investigation] = Field(default_factory=list, description="Specific investigations performed")
    quality_summary: QualitySummary = Field(..., description="Aggregated quality summary")


class ProfileListItem(BaseModel):
    """Metadata of an existing saved profile report."""
    stem: str
    file_type: str
    filename: str
    report_file: str
    created_at: str
    rows: int
    columns: int
    potential_issues: int
