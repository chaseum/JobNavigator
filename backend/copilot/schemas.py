"""Typed shapes the LLM must fill. Validated with pydantic; nothing is scraped from prose.

Every claim-bearing object carries `source_fact_ids` (provenance ids from
backend/copilot/facts.py). Deterministic code downstream decides what those ids
are worth; the model's own confidence is never a score.
"""
from typing import Literal

from pydantic import BaseModel, Field

RequirementCategory = Literal[
    "qualification", "technology", "domain", "responsibility", "education",
    "experience", "work_authorization", "clearance", "soft_skill",
]
EvidenceStatus = Literal["MATCHED", "PARTIAL", "MISSING", "UNKNOWN"]
ClaimStatus = Literal["SUPPORTED", "AMBIGUOUS", "UNSUPPORTED"]


class JobRequirement(BaseModel):
    id: str = ""
    text: str = Field(min_length=1)
    category: RequirementCategory
    required: bool
    importance: int = Field(2, ge=1, le=3)


class JobAnalysis(BaseModel):
    company: str = ""
    title: str = ""
    location: str = ""
    employment_type: str = ""
    experience_level: str = ""
    salary: str = ""
    required_qualifications: list[str] = []
    preferred_qualifications: list[str] = []
    responsibilities: list[str] = []
    technologies: list[str] = []
    domain_terms: list[str] = []
    education_requirements: list[str] = []
    experience_requirements: list[str] = []
    work_authorization_requirements: list[str] = []
    clearance_requirements: list[str] = []
    keywords: list[str] = []
    high_emphasis_concepts: list[str] = []
    requirements: list[JobRequirement] = []


class EvidenceMatch(BaseModel):
    requirement_id: str
    status: EvidenceStatus
    source_fact_ids: list[str] = []
    explanation: str = ""


class EvidenceMapping(BaseModel):
    matches: list[EvidenceMatch]


class CandidateGap(BaseModel):
    requirement_id: str
    requirement: str
    kind: Literal["safe_to_add", "safe_to_rephrase", "cannot_claim", "needs_clarification"]
    source_fact_ids: list[str] = []
    note: str = ""


class PlannedEntry(BaseModel):
    fact_id: str                          # experience_7 / internship_3 / project_4 / research_2
    bullet_sources: list[list[str]] = []  # one inner list per bullet: the fact ids it may draw on


class ResumePlan(BaseModel):
    entries: list[PlannedEntry]
    skill_ids: list[str] = []
    rationale: str = ""


class ResumeBulletRewrite(BaseModel):
    text: str = Field(min_length=1)
    source_fact_ids: list[str]
    requirement_ids: list[str] = []
    reason: str = ""


class BulletRewrites(BaseModel):
    bullets: list[ResumeBulletRewrite]


class ClaimAudit(BaseModel):
    bullet_id: str
    status: ClaimStatus
    reason: str = ""


class ResumeAudit(BaseModel):
    claims: list[ClaimAudit]


class FieldKey(BaseModel):
    field_id: str
    key: str | None = None


class FieldMapping(BaseModel):
    """Semantic fallback for form labels the deterministic patterns miss: keys only, never values."""
    mappings: list[FieldKey]


class ApplicationQuestionAnswer(BaseModel):
    answer: str = ""
    source_fact_ids: list[str] = []
    needs_user_input: bool = False
