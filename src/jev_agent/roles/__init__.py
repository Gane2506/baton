"""Role prompts (Jinja2 templates, one folder per role) and their output schemas."""

from jev_agent.roles.render import PromptError, builtin_template, load_template, render_prompt
from jev_agent.roles.schemas import (
    CODER_SUMMARY_MARKER,
    ROLE_SCHEMAS,
    CoderSummary,
    LearnerOutput,
    LessonDraft,
    LessonEdit,
    QualityReview,
    ReviewResult,
    extract_coder_summary,
)

__all__ = [
    "CODER_SUMMARY_MARKER",
    "ROLE_SCHEMAS",
    "CoderSummary",
    "LearnerOutput",
    "LessonDraft",
    "LessonEdit",
    "PromptError",
    "QualityReview",
    "ReviewResult",
    "builtin_template",
    "extract_coder_summary",
    "load_template",
    "render_prompt",
]
