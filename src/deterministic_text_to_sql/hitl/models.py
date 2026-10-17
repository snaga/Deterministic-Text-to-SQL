"""Data models for Human-in-the-Loop (HITL) ambiguity management and query parsing."""

from pydantic import BaseModel, ConfigDict, Field


class HITLOption(BaseModel):
    """An option presented to the user during Human-in-the-Loop intervention.

    Attributes:
        key: Single-letter choice key (e.g. 'A', 'B', 'C').
        label: Short human-readable display label describing the choice.
        hint: Detailed explanation or analytical context for this choice.
        clarification_text: Canonical clarification token used to disambiguate the query.
    """

    model_config = ConfigDict(frozen=True)

    key: str
    label: str
    hint: str
    clarification_text: str = ""


class HITLIssue(BaseModel):
    """An ambiguity issue requiring Human-in-the-Loop intervention.

    Attributes:
        title: Short title summarizing the ambiguity.
        question: Question prompting user selection or clarification.
        options: List of available options for resolving the ambiguity.
        base_query: The original or parsed base query that triggered this issue.
    """

    model_config = ConfigDict(frozen=True)

    title: str
    question: str
    options: list[HITLOption] = Field(default_factory=list)
    base_query: str = ""


class ParsedQueryInput(BaseModel):
    """Parsed user query input separating the base query from slash clarification syntax.

    Attributes:
        base_query: Natural language query stripped of slash directives and whitespace.
        clarification: Optional clarification specification (from slash syntax or CLI arg).
    """

    model_config = ConfigDict(frozen=True)

    base_query: str
    clarification: str | None = None
