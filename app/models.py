"""Shared data models."""
from enum import Enum

from pydantic import BaseModel, Field


class Severity(str, Enum):
    low = "low"
    medium = "medium"
    high = "high"
    critical = "critical"


SEVERITY_RANK = {Severity.low: 0, Severity.medium: 1, Severity.high: 2, Severity.critical: 3}
SEVERITY_EMOJI = {Severity.low: "💡", Severity.medium: "⚠️", Severity.high: "🔴", Severity.critical: "🚨"}


class Finding(BaseModel):
    """A single review comment anchored to a line in the new version of a file."""

    path: str
    line: int
    severity: Severity
    category: str  # bug | security | performance | style | maintainability | ...
    title: str
    body: str
    suggestion: str = ""  # optional replacement code
    source: str = "llm"  # "ast" or "llm"


# ----- Schema the LLM must return (structured output) -----

class LLMComment(BaseModel):
    path: str = Field(description="File path exactly as given in the diff header")
    line: int = Field(description="Line number in the NEW file; must be one of the numbered + or context lines shown")
    severity: Severity
    category: str = Field(description="One of: bug, security, performance, error-handling, maintainability, style, testing")
    title: str = Field(description="Short one-line summary")
    body: str = Field(description="Explanation of the problem and why it matters, in markdown")
    suggestion: str = Field(description="Replacement code for that line, or empty string if none")


class LLMReview(BaseModel):
    summary: str = Field(description="2-5 sentence overall assessment of the pull request")
    comments: list[LLMComment]


class FileDiff(BaseModel):
    path: str
    status: str
    patch: str
    content: str | None = None  # full new-file content (used for AST)
    commentable_lines: set[int] = Field(default_factory=set)
