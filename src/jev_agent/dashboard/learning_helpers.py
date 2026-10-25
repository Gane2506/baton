"""Learning actions behind the dashboard's learning POSTs."""

from __future__ import annotations

from starlette.datastructures import FormData

from jev_agent.app.learning import create_profile as learning_create
from jev_agent.app.learning import disable as learning_disable
from jev_agent.app.learning import enable as learning_enable
from jev_agent.app.learning import forget as learning_forget
from jev_agent.app.learning import graph_link as learning_graph_link
from jev_agent.app.learning import graph_unlink as learning_graph_unlink
from jev_agent.app.learning import use_learning_profile
from jev_agent.core.learning import GRAPH_EDGE_TYPES, LearningError
from jev_agent.dashboard import views
from jev_agent.dashboard.helpers import _text


def _learning_create(repo: views.Repo, form: FormData) -> str:
    name = _text(form, "name") or ""
    profile = learning_create(repo.learning, name, shared=form.get("shared") == "on")
    return f"created profile {profile.name} ({profile.scope})"


def _learning_use(repo: views.Repo, form: FormData) -> str:
    name = _text(form, "profile") or ""
    use_learning_profile(repo.store, repo.learning, name)
    return f"active profile: {name}"


def _learning_enable(repo: views.Repo, form: FormData) -> str:
    name = _text(form, "profile") or None
    active = learning_enable(repo.store, repo.config, repo.learning, name)
    if name is None:
        return f"learning is on for this repo, with profile {active}"
    return f"profile {name} is enabled"


def _learning_disable(repo: views.Repo, form: FormData) -> str:
    name = _text(form, "profile") or None
    learning_disable(repo.store, repo.learning, name)
    if name is None:
        return "learning is off for this repo; lessons are kept"
    return f"profile {name} is disabled"


def _learning_forget(repo: views.Repo, form: FormData) -> str:
    lesson = learning_forget(repo.learning, _text(form, "lesson") or "")
    return f"retired {lesson.id} from {lesson.profile}"


def _learning_edit(repo: views.Repo, form: FormData) -> str:
    """Edit a lesson's text, roles, and tags (issue #161)."""
    from jev_agent.core.learning import LearningError

    lesson_ref = _text(form, "lesson") or ""
    text = _text(form, "text") or ""
    roles = [r.strip() for r in (_text(form, "roles") or "").split(",") if r.strip()]
    tags = [t.strip() for t in (_text(form, "tags") or "").split(",") if t.strip()]
    try:
        db, lesson = repo.learning.find_lesson(lesson_ref)
    except LearningError as exc:
        return f"error: {exc}"
    db.update_lesson(lesson.profile, lesson.number, text, roles, tags)
    return f"updated {lesson.id}"


def _learning_delete_lesson(repo: views.Repo, form: FormData) -> str:
    """Permanently delete a lesson (issue #161)."""
    from jev_agent.core.learning import LearningError

    lesson_ref = _text(form, "lesson") or ""
    try:
        db, lesson = repo.learning.find_lesson(lesson_ref)
    except LearningError as exc:
        return f"error: {exc}"
    db.delete_lesson(lesson.profile, lesson.number)
    return f"deleted {lesson.id} from {lesson.profile}"


def _learning_graph_link(repo: views.Repo, form: FormData) -> str:
    """Record a graph edge from the Graph tab (issue #254): `link L31 supersedes L12`,
    `link L4 contradicts L9`, `link L12 applies-to src/auth/`. `LearningError` for an
    unknown lesson, a duplicate edge, or a bad edge type — the route flashes it."""
    src = _text(form, "src") or ""
    edge = _text(form, "edge") or ""
    dst = _text(form, "dst") or ""
    if edge not in GRAPH_EDGE_TYPES:
        raise LearningError(f"unknown edge {edge!r} (use: {', '.join(GRAPH_EDGE_TYPES)})")
    recorded = learning_graph_link(repo.learning, src, edge, dst)
    note = (
        " (advisory: the old lesson stays active until a human retires it)"
        if edge == "supersedes"
        else " (advisory: both lessons stay active)"
        if edge == "contradicts"
        else ""
    )
    return f"Recorded: {recorded.src} {recorded.edge} {recorded.dst}.{note}"


def _learning_graph_unlink(repo: views.Repo, form: FormData) -> str:
    """Remove a recorded graph edge from the Graph tab (issue #254). `contradicts`
    matches in either direction. `LearningError` when there is no such edge, so the
    route flashes an error instead of claiming a removal that never happened."""
    src = _text(form, "src") or ""
    edge = _text(form, "edge") or ""
    dst = _text(form, "dst") or ""
    if edge not in GRAPH_EDGE_TYPES:
        raise LearningError(f"unknown edge {edge!r} (use: {', '.join(GRAPH_EDGE_TYPES)})")
    if learning_graph_unlink(repo.learning, src, edge, dst):
        return f"Removed: {src} {edge} {dst}."
    raise LearningError(f"No such edge: {src} {edge} {dst}.")


def _learning_approve_skill(repo: views.Repo, form: FormData) -> str:
    """Approve a proposed skill: add it to the skills library (issue #161)."""
    from jev_agent.core.learning import LearningError

    profile = _text(form, "profile") or ""
    name = _text(form, "name") or ""
    description = _text(form, "description") or ""
    raw_ids = _text(form, "lesson_ids") or ""
    lesson_ids = [lid.strip() for lid in raw_ids.split(",") if lid.strip()]
    try:
        db = repo.learning.db_for(profile)
    except LearningError as exc:
        return f"error: {exc}"
    try:
        skill = db.add_skill(profile, name, description, lesson_ids, source="learned")
    except LearningError as exc:
        return f"error: {exc}"
    return f"created skill '{skill.name}' from {len(lesson_ids)} lesson(s)"


def _learning_delete_skill(repo: views.Repo, form: FormData) -> str:
    """Delete a skill (issue #161)."""
    from jev_agent.core.learning import LearningError

    profile = _text(form, "profile") or ""
    skill_id = _text(form, "skill_id") or ""
    try:
        db = repo.learning.db_for(profile)
        db.delete_skill(profile, int(skill_id))
    except (LearningError, ValueError) as exc:
        return f"error: {exc}"
    return f"deleted skill {skill_id} from {profile}"
