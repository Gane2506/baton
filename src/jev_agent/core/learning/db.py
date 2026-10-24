"""`LessonDB`: profiles and lessons in one SQLite database (the repo's state.db,
or the shared one). The class itself is only the composition of its mixins —
profiles in `profiles.py`, lessons in `lessons.py`, skills in `skills.py`,
the graph in `graph.py`, shared plumbing in `base.py`.
"""

from __future__ import annotations

import os
from pathlib import Path

from jev_agent.core.learning.graph import LessonGraphMixin
from jev_agent.core.learning.lessons import LessonLessonsMixin
from jev_agent.core.learning.profiles import LessonProfilesMixin
from jev_agent.core.learning.skills import LessonSkillsMixin


def shared_db_path() -> Path:
    """`$XDG_DATA_HOME/jev/learning.db`, falling back to `~/.local/share/jev/learning.db`."""
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "jev" / "learning.db"


class LessonDB(LessonProfilesMixin, LessonLessonsMixin, LessonSkillsMixin, LessonGraphMixin):
    """Profiles and lessons in one SQLite database (the repo's state.db, or the shared one)."""


__all__ = ["LessonDB", "shared_db_path"]
