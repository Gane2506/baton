"""Recursive learning: profiles and lessons (repo and shared scope), settings precedence,
selection, D7 rules, applying the learner's output, and scoring."""

from collections.abc import Iterator
from pathlib import Path
from typing import Any, get_args

import pytest

from jev_agent.config import JevConfig
from jev_agent.core.learner import (
    MAX_ADDS,
    apply_learner_output,
    learning_signals,
    run_card,
    score_lessons,
)
from jev_agent.core.learning import (
    Learning,
    LearningError,
    Lesson,
    context_terms,
    learning_status,
    resolve_learning,
    select_lessons,
    set_active_profile,
    set_repo_learning,
    shared_db_path,
)
from jev_agent.core.models import (
    Decision,
    DecisionPoint,
    Idea,
    Plan,
    RunState,
    TaskCard,
    TaskState,
    WorkerResult,
)
from jev_agent.core.store import Store
from jev_agent.dispatch.jev import POINT_DESCRIPTIONS, JevDispatcher
from jev_agent.dispatch.requests import RoleOptions, d5_after_attempt, d7_learn
from jev_agent.dispatch.rules import RulesDispatcher
from jev_agent.roles import LearnerOutput, LessonDraft, LessonEdit


@pytest.fixture
def store(tmp_path: Path) -> Iterator[Store]:
    with Store.open(tmp_path / "repo" / ".jev" / "state.db") as s:
        yield s


@pytest.fixture
def learning(store: Store) -> Iterator[Learning]:
    lr = Learning.for_store(store)
    yield lr
    lr.close()


def _config(**learning: Any) -> JevConfig:
    return JevConfig.model_validate({"learning": learning} if learning else {})


# ---- profiles and lessons ------------------------------------------------------------------


def test_repo_profile_and_lesson_round_trip(learning: Learning) -> None:
    profile = learning.create("work")
    assert (profile.name, profile.scope, profile.enabled, profile.lessons) == (
        "work",
        "repo",
        True,
        0,
    )
    db = learning.db_for("work")
    lesson = db.add_lesson("work", "Run tests from the root.", ["coder"], ["tests"], "r1", "T1")
    assert lesson.id == "L1"
    got = db.get_lesson(1)
    assert got == lesson
    assert (got.roles, got.tags, got.source_run, got.source_task) == (
        ("coder",),
        ("tests",),
        "r1",
        "T1",
    )
    assert (got.uses, got.score, got.retired) == (0, 0.0, False)
    assert learning.get("work") is not None and learning.get("work").lessons == 1  # type: ignore[union-attr]
    db.update_text("work", 1, "Run `pytest` from the root.")
    assert learning.lessons("work")[0].text == "Run `pytest` from the root."
    assert not shared_db_path().exists()  # reading and repo writes never create the shared db


def test_shared_profile_lives_in_xdg_data_home(
    learning: Learning, store: Store, tmp_path: Path
) -> None:
    learning.create("team", shared=True)
    path = tmp_path / "xdg-data" / "jev" / "learning.db"
    assert shared_db_path() == path and path.exists()
    lesson = learning.db_for("team").add_lesson("team", "Shared lesson.", ["planner"], [])
    assert lesson.id == "S1"
    # Another repo's store sees the shared profile and lesson.
    with Store.open(tmp_path / "other" / ".jev" / "state.db") as other_store:
        other = Learning.for_store(other_store)
        try:
            assert [p.name for p in other.profiles()] == ["team"]
            assert other.get("team").scope == "shared"  # type: ignore[union-attr]
            assert [x.text for x in other.lessons("team")] == ["Shared lesson."]
            assert other.forget("S1").id == "S1"
        finally:
            other.close()
    assert learning.lessons("team") == []
    with pytest.raises(LearningError, match="already exists"):
        learning.create("team")


def test_enable_disable_flush_delete_forget(learning: Learning) -> None:
    learning.create("p")
    db = learning.db_for("p")
    for text in ("a lesson", "b lesson"):
        db.add_lesson("p", text, ["coder"], [])
    learning.set_enabled("p", False)
    assert learning.get("p").enabled is False  # type: ignore[union-attr]
    learning.set_enabled("p", True)
    assert learning.forget("l2").id == "L2"  # ids are case-insensitive
    assert [x.id for x in learning.lessons("p")] == ["L1"]
    assert [x.id for x in learning.lessons("p", include_retired=True)] == ["L1", "L2"]
    assert learning.flush("p") == 2
    assert learning.lessons("p", include_retired=True) == []
    learning.delete("p")
    assert learning.get("p") is None
    with pytest.raises(LearningError, match="no learning profile"):
        learning.flush("p")
    with pytest.raises(LearningError, match="no lesson"):
        learning.forget("L99")
    with pytest.raises(LearningError, match="no lesson"):
        learning.forget("S1")  # no shared database yet
    with pytest.raises(LearningError, match="not a valid profile name"):
        learning.create("bad name")


# ---- which profile a run uses ----------------------------------------------------------------


def test_learning_is_off_by_default(store: Store, learning: Learning) -> None:
    assert resolve_learning(store, learning, JevConfig()) is None
    status = learning_status(store, JevConfig())
    assert (status.enabled, status.enabled_source, status.profile, status.profile_source) == (
        False,
        "default",
        "default",
        "default",
    )
    assert learning.profiles() == []  # nothing is created while learning is off


def test_precedence_flag_over_setting_over_config(store: Store, learning: Learning) -> None:
    config = _config(enabled=True, profile="cfg")
    # jev.toml turns learning on; its profile is created on first use.
    assert resolve_learning(store, learning, config) == "cfg"
    assert learning.get("cfg") is not None
    assert learning_status(store, config).enabled_source == "jev.toml"
    # A CLI setting beats jev.toml...
    learning.create("cli")
    set_active_profile(store, "cli")
    assert resolve_learning(store, learning, config) == "cli"
    set_repo_learning(store, False)
    assert resolve_learning(store, learning, config) is None
    assert learning_status(store, config).enabled_source == "the switch"
    # ...and run flags beat both.
    learning.create("flag")
    assert resolve_learning(store, learning, config, flag_profile="flag") == "flag"
    set_repo_learning(store, True)
    assert resolve_learning(store, learning, config, no_learning=True) is None
    with pytest.raises(LearningError, match="cannot be used together"):
        resolve_learning(store, learning, config, "flag", no_learning=True)
    with pytest.raises(LearningError, match="no learning profile named 'nope'"):
        resolve_learning(store, learning, config, flag_profile="nope")
    # A disabled active profile means no learning, unless a run names it.
    learning.set_enabled("cli", False)
    assert resolve_learning(store, learning, config) is None
    assert resolve_learning(store, learning, config, flag_profile="cli") == "cli"


def test_config_validates_learning() -> None:
    cfg = JevConfig()
    assert (cfg.learning.enabled, cfg.learning.profile) == (False, "default")
    assert (cfg.learning.max_lessons, cfg.learning.max_chars) == (8, 2000)
    assert cfg.roles.learner.workers == ["claude_code", "api", "codex"]
    with pytest.raises(ValueError, match="profile"):
        _config(profile="has space")


# ---- selection -------------------------------------------------------------------------------


def _lesson(
    n: int, text: str, roles: tuple[str, ...], tags: tuple[str, ...], score: float = 0
) -> Lesson:
    return Lesson(
        id=f"L{n}",
        profile="p",
        text=text,
        roles=roles,
        tags=tags,
        source_run=None,
        source_task=None,
        created_at=None,  # type: ignore[arg-type]
        uses=0,
        score=score,
        retired=False,
    )


def test_context_terms() -> None:
    terms = context_terms(["Add rate limiting to the API"], ["src/jev_agent/api.py"])
    assert {
        "rate",
        "limiting",
        "api",
        "src",
        "src/jev_agent",
        "src/jev_agent/api.py",
        "jev_agent",
        "api.py",
    } <= terms
    assert "the" not in terms and "to" not in terms


def test_selection_ranks_by_overlap_then_score_then_recency() -> None:
    lessons = [
        _lesson(1, "general old", ("coder",), (), score=0),
        _lesson(2, "general new", ("coder",), (), score=0),
        _lesson(3, "high score", ("coder",), (), score=2),
        _lesson(4, "api lesson", ("coder",), ("src/jev_agent/api.py",), score=-1),
        _lesson(5, "two tags", ("coder",), ("api", "rate limiting"), score=-1),
        _lesson(6, "planner only", ("planner",), ("api",)),
        _lesson(7, "other area", ("coder",), ("docs",), score=5),
    ]
    terms = context_terms(["rate limiting"], ["src/jev_agent/api.py"])
    picked = select_lessons(lessons, "coder", terms, max_lessons=10, max_chars=10_000)
    assert [x.id for x in picked] == ["L5", "L4", "L7", "L3", "L2", "L1"]
    assert [x.id for x in select_lessons(lessons, "planner", terms, 10, 10_000)] == ["L6"]
    assert len(select_lessons(lessons, None, terms, 10, 10_000)) == 7


def test_selection_caps_count_and_chars() -> None:
    lessons = [
        _lesson(1, "x" * 50, ("coder",), ("api",)),
        _lesson(2, "y" * 30, ("coder",), ()),
        _lesson(3, "z" * 10, ("coder",), ()),
    ]
    terms = {"api"}
    assert [x.id for x in select_lessons(lessons, "coder", terms, 2, 1000)] == ["L1", "L3"]
    # L1 does not fit in 45 chars; smaller lessons after it still do.
    assert [x.id for x in select_lessons(lessons, "coder", terms, 8, 45)] == ["L3", "L2"]
    assert select_lessons(lessons, "coder", terms, 0, 1000) == []


# ---- D7 --------------------------------------------------------------------------------------

LEARNER = RoleOptions("learner", ["claude_code", "api"], ["default"], ["low", "medium", "high"])


@pytest.mark.parametrize(
    ("outcome", "signals", "lessons", "expected"),
    [
        ("DONE", [], 0, "learn"),  # clean run, nearly empty profile
        ("DONE", [], 2, "learn"),
        ("DONE", [], 3, "skip"),  # clean run, profile has enough lessons
        ("DONE", ["retry"], 10, "learn"),
        ("DONE", ["revision"], 10, "learn"),
        ("FAILED", ["failed"], 10, "learn"),
        ("NEEDS_HUMAN", ["needs_human"], 10, "learn"),
    ],
)
async def test_d7_rules(outcome: str, signals: list[str], lessons: int, expected: str) -> None:
    req = d7_learn(outcome, signals, 2, lessons, LEARNER, 0.5, 5.0)
    decision = await RulesDispatcher().decide(req)
    assert decision.answers == {
        "learn": expected,
        "worker": "claude_code",
        "model": "default",
        "effort": "medium",
    }
    assert decision.invalid_answers(req) == []


def test_jev_knows_every_decision_point() -> None:
    assert set(POINT_DESCRIPTIONS) == set(get_args(DecisionPoint))
    jev = JevDispatcher()
    assert "signals" in jev.instruction_for("D7", "learn")
    payload = jev.build_payload("D7", {"outcome": "DONE"}, {"learn": ["learn", "skip"]})
    assert payload["questions"]["learn"]["criteria"]["skip"]


# ---- applying the learner's output --------------------------------------------------------------


def test_apply_learner_output_limits_and_redacts(
    learning: Learning, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MY_API_KEY", "supersecretvalue123")
    learning.create("p")
    db = learning.db_for("p")
    old = db.add_lesson("p", "Old lesson.", ["coder"], [])
    hidden = db.add_lesson("p", "Not shown.", ["coder"], [])
    output = LearnerOutput(
        add=[
            LessonDraft(text=f"Lesson {i}.", roles=["coder", "bogus"], tags=[" SRC/Api/ ", "x"])
            for i in range(MAX_ADDS + 2)
        ]
        + [LessonDraft(text="old  LESSON.", roles=["coder"])],
        update=[
            LessonEdit(id="l1", text="Use key supersecretvalue123 and sk-abcdefghijklmnop"),
            LessonEdit(id=hidden.id, text="hijack"),
        ],
        retire=[hidden.id],
    )
    applied = apply_learner_output(db, "p", output, [old], run_id="r1")
    assert len(applied.added) == MAX_ADDS
    assert applied.updated == ["L1"] and applied.retired == []  # only shown lessons change
    first = db.get_lesson(int(applied.added[0][1:]))
    assert first is not None
    assert (first.roles, first.tags, first.source_run) == (("coder",), ("src/api", "x"), "r1")
    text = db.get_lesson(old.number).text  # type: ignore[union-attr]
    assert "supersecretvalue123" not in text and "sk-abcdefghijklmnop" not in text
    assert "[REDACTED]" in text
    assert db.get_lesson(hidden.number).text == "Not shown."  # type: ignore[union-attr]


def test_apply_learner_output_clips_and_defaults_roles(learning: Learning) -> None:
    learning.create("p")
    db = learning.db_for("p")
    shown = [db.add_lesson("p", "Retire me.", ["coder"], [])]
    output = LearnerOutput(
        add=[LessonDraft(text="word " * 100, roles=[]), LessonDraft(text="   ", roles=["coder"])],
        retire=["L1", "L1"],
    )
    applied = apply_learner_output(db, "p", output, shown)
    assert applied.retired == ["L1"] and len(applied.added) == 1
    lesson = db.get_lesson(int(applied.added[0][1:]))
    assert lesson is not None and len(lesson.text) == 300 and "\n" not in lesson.text
    assert lesson.roles == ("planner", "validator", "coder", "reviewer")
    assert db.get_lesson(1).retired  # type: ignore[union-attr]


# ---- scoring -------------------------------------------------------------------------------------


def _card(tid: str) -> TaskCard:
    return TaskCard(id=tid, title=f"task {tid}", description="d", files_hint=["src/a.py"])


def _attempt(store: Store, run_id: str, task_id: str, n: int, lessons: list[str]) -> None:
    aid = store.start_attempt(run_id, task_id, n, "coder", "fake", lesson_ids=lessons)
    store.finish_attempt(aid, WorkerResult(status="success", summary="ok", raw_log_path=Path()))


def test_scoring_updates_uses_and_retires(store: Store, learning: Learning) -> None:
    learning.create("p")
    db = learning.db_for("p")
    good = db.add_lesson("p", "good", ["coder"], [])
    bad = db.add_lesson("p", "bad", ["coder"], [])
    for round_no in range(4):
        run = store.create_run(Idea(text="x", repo_path=Path(".")), "p")
        store.upsert_tasks(run.id, Plan(summary="s", tasks=[_card("T1"), _card("T2")]))
        _attempt(store, run.id, "T1", 1, [good.id])  # done on the first attempt
        _attempt(store, run.id, "T2", 1, [bad.id])
        _attempt(store, run.id, "T2", 2, [bad.id])  # needed a retry
        store.set_task_state(run.id, "T1", TaskState.DONE)
        store.set_task_state(run.id, "T2", TaskState.DONE)
        for _ in range(2):
            store.increment_task_attempts(run.id, "T2")
        store.increment_task_attempts(run.id, "T1")
        score_lessons(store, store.get_run(run.id), db)
        score_lessons(store, store.get_run(run.id), db)  # idempotent per task
        b = db.get_lesson(bad.number)
        assert b is not None and b.uses == round_no + 1 and b.score == -0.5 * (round_no + 1)
    g = db.get_lesson(good.number)
    b = db.get_lesson(bad.number)
    assert g is not None and (g.uses, g.score, g.retired) == (4, 4.0, False)
    assert b is not None and b.retired  # score reached -2
    assert [x.id for x in learning.lessons("p")] == [good.id]


def test_run_card_and_signals(store: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SOME_TOKEN", "tok-value-12345678")
    run = store.create_run(Idea(text="add search", repo_path=Path(".")), "p")
    store.upsert_tasks(run.id, Plan(summary="plan", tasks=[_card("T1")]))
    store.set_task_state(run.id, "T1", TaskState.DONE, note="boom: tok-value-12345678 leaked")
    store.increment_task_attempts(run.id, "T1")
    store.increment_task_attempts(run.id, "T1")
    req = d5_after_attempt(
        _card("T1"),
        attempt=1,
        max_attempts=3,
        assignment={"worker": "w", "model": None, "effort": None},
        worker_status="failure",
        summary="s",
        tests="failed",
        review="skipped",
        can_reassign=True,
        task_spend_usd=0,
        task_budget_usd=1,
    )
    store.record_decision(
        run.id, "T1", req, Decision(point="D5", answers={"action": "retry"}, dispatcher="rules")
    )
    store.set_run_state(run.id, RunState.DONE)
    run = store.get_run(run.id)
    assert learning_signals(store, run) == ["retry"]
    card = run_card(store, run)
    assert card["outcome"] == "DONE" and card["signals"] == ["retry"]
    task = card["tasks"][0]
    assert task["d5_actions"] == ["retry"] and task["attempts"] == 2
    assert "tok-value-12345678" not in task["failure"] and "[REDACTED]" in task["failure"]


def test_store_records_learning_columns(store: Store) -> None:
    run = store.create_run(Idea(text="x", repo_path=Path(".")), "default")
    assert (run.learning_profile, run.learning_outcome) == ("default", None)
    store.set_run_learning_outcome(run.id, "learned")
    store.set_run_learning(run.id, None)
    run = store.get_run(run.id)
    assert (run.learning_profile, run.learning_outcome) == (None, "learned")
    aid = store.start_attempt(run.id, None, 1, "planner", "fake", lesson_ids=["L1", "S2"])
    assert store.get_attempt(aid).lesson_ids == ("L1", "S2")
    assert store.get_setting("k") is None
    store.set_setting("k", "v")
    store.set_setting("k", "w")
    assert store.get_setting("k") == "w"
    store.set_setting("k", None)
    assert store.get_setting("k") is None


# ---- lesson ids and scoring stay with their profile (#40) ----------------------------------------


@pytest.mark.parametrize("shared", [False, True])
def test_lesson_ids_are_never_reused_after_flush(learning: Learning, shared: bool) -> None:
    learning.create("a", shared=shared)
    learning.create("b", shared=shared)
    db = learning.db_for("a")
    first = db.add_lesson("a", "old lesson", ["coder"], [])
    assert learning.flush("a") == 1
    second = db.add_lesson("b", "new lesson", ["coder"], [])
    assert second.number > first.number
    with pytest.raises(LearningError, match="no lesson"):
        learning.forget(first.id)  # the flushed id points at nothing, never at b's lesson


def test_lesson_writes_only_touch_the_given_profile(learning: Learning) -> None:
    learning.create("a")
    learning.create("b")
    db = learning.db_for("b")
    lesson = db.add_lesson("b", "b's lesson", ["coder"], [])
    db.record_use("a", lesson.number, -5.0)
    db.update_text("a", lesson.number, "changed")
    db.retire("a", lesson.number)
    assert db.get_lesson(lesson.number) == lesson  # untouched: it belongs to b
    db.record_use("b", lesson.number, -0.5)
    got = db.get_lesson(lesson.number)
    assert got is not None and (got.uses, got.score) == (1, -0.5)


def test_shared_scoring_is_not_repeated_after_a_crash(
    store: Store, learning: Learning, monkeypatch: pytest.MonkeyPatch
) -> None:
    learning.create("team", shared=True)
    db = learning.db_for("team")
    lesson = db.add_lesson("team", "shared lesson", ["coder"], [])
    run = store.create_run(Idea(text="x", repo_path=Path(".")), "team")
    store.upsert_tasks(run.id, Plan(summary="s", tasks=[_card("T1")]))
    _attempt(store, run.id, "T1", 1, [lesson.id])
    store.set_task_state(run.id, "T1", TaskState.DONE)
    store.increment_task_attempts(run.id, "T1")

    def crash(run_id: str, task_id: str) -> None:
        raise KeyboardInterrupt  # the process dies after scoring, before the mark

    with monkeypatch.context() as m:
        m.setattr(store, "mark_task_lessons_scored", crash)
        with pytest.raises(KeyboardInterrupt):
            score_lessons(store, store.get_run(run.id), db)
    assert not store.get_task(run.id, "T1").lessons_scored
    score_lessons(store, store.get_run(run.id), db)  # resume scores again
    got = db.get_lesson(lesson.number)
    assert got is not None and (got.uses, got.score) == (1, 1.0)
    assert store.get_task(run.id, "T1").lessons_scored


def test_shared_migration_rebuilds_lessons_without_reusing_ids(tmp_path: Path) -> None:
    import sqlite3

    from jev_agent.core.learning import LessonDB
    from jev_agent.core.migrations import SHARED_MIGRATIONS

    path = tmp_path / "learning.db"
    with sqlite3.connect(path) as conn:
        conn.executescript(SHARED_MIGRATIONS[0] + "\nPRAGMA user_version = 1;")
        conn.execute("INSERT INTO learning_profiles VALUES ('a', 1, '2026-10-01T00:00:00+00:00')")
        for number in (1, 2, 3):
            conn.execute(
                "INSERT INTO lessons (id, profile, text, roles_json, tags_json, created_at,"
                " updated_at, uses, score) VALUES (?, 'a', ?, '[\"coder\"]', '[]', ?, ?, 2, 1.5)",
                (number, f"lesson {number}", *["2026-10-01T00:00:00+00:00"] * 2),
            )
        conn.execute("DELETE FROM lessons WHERE id = 2")
    db = LessonDB.open_shared(path)
    try:
        assert db.conn.execute("PRAGMA user_version").fetchone()[0] == len(SHARED_MIGRATIONS)
        assert [(x.id, x.text, x.uses, x.score) for x in db.list_lessons("a")] == [
            ("S1", "lesson 1", 2, 1.5),
            ("S3", "lesson 3", 2, 1.5),
        ]
        db.flush("a")
        assert db.add_lesson("a", "after", ["coder"], []).id == "S4"
        assert db.conn.execute("PRAGMA foreign_key_check").fetchall() == []
        with db.conn:
            db.conn.execute("DELETE FROM learning_profiles WHERE name = 'a'")
        assert db.list_lessons("a", include_retired=True) == []  # still cascades
    finally:
        db.conn.close()
