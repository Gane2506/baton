"""Recursive learning: profiles of lessons, choosing which lessons a prompt gets,
and deciding which profile (if any) a run uses. See docs/learning.md.

- A **profile** is a named store of **lessons**. Scope `repo` keeps it in the repo's state.db
  (`.jev/state.db`); scope `shared` keeps it in `$XDG_DATA_HOME/jev/learning.db` (else
  `~/.local/share/jev/learning.db`), usable from every repo. Names are unique across scopes;
  lookups try the repo first. Lesson ids carry their scope: `L12` (repo), `S3` (shared), and
  are never reused once deleted.
- **Selection** (`select_lessons`) is deterministic: lessons for the role, not retired, ranked
  by how many of their tags match the context (task files and title words, idea words), then
  by score, then newest first; capped by `[learning] max_lessons` and `max_chars`.
- **Resolution** (`resolve_learning`): a run flag beats the repo setting stored by
  `jev learning` beats `jev.toml` beats the defaults.

The distill step (the learner role, D7, scoring) lives in `core/learner.py`.

Split from the single `core/learning.py` god-file (issue #222) into focused
modules; this package re-exports the public API unchanged:

- `models` — constants, `LearningError`, and the frozen dataclasses
- `paths` — module-path helpers (`normalize_module_path`, `repo_slug_for`, ...)
- `weights` — learning-profile Laya weights, including the NaN guard
- `base` / `profiles` / `lessons` / `skills` / `graph` — the `LessonDB` mixins
- `db` — `LessonDB` itself (the mixin composition)
- `registry` — `Learning`, every profile visible from one repo
- `resolution` — which profile a run uses (`resolve_learning`, `learning_status`)
- `selection` — `context_terms`, `select_lessons`, `suppress_contradictions`
"""

from jev_agent.core.learning.base import LessonDBBase
from jev_agent.core.learning.db import LessonDB, shared_db_path
from jev_agent.core.learning.graph import LessonGraphMixin, co_injected
from jev_agent.core.learning.lessons import LessonLessonsMixin
from jev_agent.core.learning.models import (
    GRAPH_EDGE_TYPES,
    MAX_LESSON_CHARS,
    RETIRE_AT_SCORE,
    SETTING_ENABLED,
    SETTING_PROFILE,
    EdgeView,
    GraphStats,
    LearningError,
    Lesson,
    LessonEdge,
    Profile,
    Scope,
    Skill,
    Suppression,
)
from jev_agent.core.learning.paths import (
    normalize_module_path,
    normalize_module_ref,
    qualify_module,
    repo_slug_for,
    split_module_ref,
)
from jev_agent.core.learning.profiles import LessonProfilesMixin, check_profile_name
from jev_agent.core.learning.registry import Learning
from jev_agent.core.learning.resolution import (
    LearningStatus,
    delete_profile,
    disable_learning,
    enable_learning,
    learning_status,
    resolve_learning,
    set_active_profile,
    set_repo_learning,
    use_profile,
)
from jev_agent.core.learning.selection import (
    context_terms,
    normalize_tag,
    select_lessons,
    suppress_contradictions,
    tag_overlap,
)
from jev_agent.core.learning.skills import LessonSkillsMixin, SkillProposal
from jev_agent.core.learning.weights import _checked_weights
from jev_agent.core.models import LESSON_ROLES

__all__ = [
    "GRAPH_EDGE_TYPES",
    "LESSON_ROLES",
    "MAX_LESSON_CHARS",
    "RETIRE_AT_SCORE",
    "SETTING_ENABLED",
    "SETTING_PROFILE",
    "EdgeView",
    "GraphStats",
    "Learning",
    "LearningError",
    "LearningStatus",
    "Lesson",
    "LessonDB",
    "LessonDBBase",
    "LessonEdge",
    "LessonGraphMixin",
    "LessonLessonsMixin",
    "LessonProfilesMixin",
    "LessonSkillsMixin",
    "Profile",
    "Scope",
    "Skill",
    "SkillProposal",
    "Suppression",
    "_checked_weights",
    "check_profile_name",
    "co_injected",
    "context_terms",
    "delete_profile",
    "disable_learning",
    "enable_learning",
    "learning_status",
    "normalize_module_path",
    "normalize_module_ref",
    "normalize_tag",
    "qualify_module",
    "repo_slug_for",
    "resolve_learning",
    "select_lessons",
    "set_active_profile",
    "set_repo_learning",
    "shared_db_path",
    "split_module_ref",
    "suppress_contradictions",
    "tag_overlap",
    "use_profile",
]
