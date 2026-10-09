"""Contract types for CPG metadata — guideline-level information.

Registered once per ingested guideline. Both decision models and
recommendations reference the CPG by cpg_id.
"""

from datetime import date
from enum import Enum

from pydantic import BaseModel


CONTRACT_VERSION = "1.1"


class GradingSystem(str, Enum):
    GRADE = "GRADE"
    COR_LOE = "COR-LOE"
    GRADE_COR_HYBRID = "GRADE-COR-hybrid"
    SIMPLIFIED = "simplified"
    VERB_IMPLIED = "verb-implied"
    UNGRADED = "ungraded"


class CPGMetadata(BaseModel):
    contract_version: str = CONTRACT_VERSION
    cpg_id: str
    title: str
    version: str | None = None
    publication_date: date | None = None
    evidence_review_date: date | None = None
    issuing_body: str | None = None
    grading_system: GradingSystem | None = None
    scope: str | None = None
    supersedes: list[str] | None = None
