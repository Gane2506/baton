"""The queryable lesson graph: migration, edge CRUD, FTS, the learner's declarations, the
evidence-gated contradiction rule, the CLI, and the dashboard's Graph tab."""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

pytest.importorskip("starlette", reason="dashboard extra not installed")

from starlette.testclient import TestClient
from typer.testing import CliRunner

from jev_agent.app.learning import (
    graph_link,
    graph_query,
    graph_show,
    graph_stats,
    graph_unlink,
)
from jev_agent.cli.app import app
from jev_agent.core.learner import MAX_EDGES_PER_RUN, apply_learner_output
from jev_agent.core.learning import (
    Learning,
    LearningError,
    LessonDB,
    co_injected,
    normalize_module_path,
    qualify_module,
    repo_slug_for,
    select_lessons,
    split_module_ref,
    suppress_contradictions,
)
from jev_agent.core.migrations import SHARED_MIGRATIONS
from jev_agent.core.store import Store
from jev_agent.dashboard.app import create_app
from jev_agent.dashboard.security import DashboardSettings
from jev_agent.roles import LearnerOutput, LessonDraft

runner = CliRunner()

CONFIG = '[dispatcher]\nprimary = "rules"\n[approval]\nrequire_plan_approval = false\n'


@pytest.fixture
def repo(fixture_repo: Path) -> Path:
    (fixture_repo / "jev.toml").write_text(CONFIG)
    return fixture_repo


@pytest.fixture
def shared_db(tmp_path: Path) -> Iterator[LessonDB]:
    """A shared-scope lesson database (the graph migration applies on open)."""
    db = LessonDB.open_shared(tmp_path / "learning.db")
    db.create_profile("work")
    yield db
    db.conn.close()


def _jev(repo: Path, *args: str, input: str | None = None) -> tuple[int, str]:
    result = runner.invoke(app, [*args, "--repo", str(repo)], input=input)
    return result.exit_code, " ".join(re.sub(r"[\u2500-\u257f]", " ", result.output).split())


# ---- migration ---------------------------------------------------------------------------------


def test_shared_migration_is_v4_and_backfills(tmp_path: Path) -> None:
    path = tmp_path / "learning.db"
    with sqlite3.connect(path) as conn:
        conn.executescript(SHARED_MIGRATIONS[0] + "\nPRAGMA user_version = 1;")
        conn.execute("INSERT INTO learning_profiles VALUES ('a', 1, '2026-10-01T00:00:00+00:00')")
        conn.execute(
            "INSERT INTO lessons (id, profile, text, roles_json, tags_json, source_run,"
            " created_at, updated_at, uses, score) VALUES"
            " (1, 'a', 'Batch API calls to stay fast', '[\"coder\"]',"
            " '[\"src/api/batch.py\", \"perf\"]', 'r1', 't', 't', 2, 1.5)"
        )
    db = LessonDB.open_shared(path)
    try:
        assert db.conn.execute("PRAGMA user_version").fetchone()[0] == 5
        assert db.graph_ready()
        # FTS backfilled: the old lesson is searchable.
        assert [
            row[0]
            for row in db.conn.execute(
                "SELECT rowid FROM lessons_fts WHERE lessons_fts MATCH 'batch'"
            )
        ] == [1]
        # applies-to backfilled from the path-looking tag, marked backfill.
        edges = [(e.src, e.edge, e.dst, e.origin) for e in db.lesson_edges("a")]
        assert ("S1", "applies-to", "src/api/batch.py", "backfill") in edges
        # derived-from is a view over source_run.
        assert [row[0] for row in db.conn.execute("SELECT dst_id FROM lesson_run_edges")] == ["r1"]
        # module node exists.
        assert [row[0] for row in db.conn.execute("SELECT path FROM lesson_modules")] == [
            "src/api/batch.py"
        ]
    finally:
        db.conn.close()


def test_repo_migration_21_adds_graph_tables_and_backfills(tmp_path: Path) -> None:
    """Repo databases gain the lesson graph in migration 21 (issue #319): a repo
    profile's Graph tab no longer degrades to List."""
    from jev_agent.core.migrations import MIGRATIONS, apply_migrations

    path = tmp_path / "state.db"
    conn = sqlite3.connect(path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    try:
        apply_migrations(conn, migrations=MIGRATIONS[:20])
        conn.execute(
            "INSERT INTO learning_profiles (name, enabled, created_at)"
            " VALUES ('p', 1, '2026-10-01T00:00:00+00:00')"
        )
        conn.execute(
            "INSERT INTO lessons (id, profile, text, roles_json, tags_json, source_run,"
            " created_at, updated_at, uses, score) VALUES"
            " (1, 'p', 'Batch API calls to stay fast', '[\"coder\"]',"
            " '[\"src/api/batch.py\", \"perf\"]', 'r1', '2026-10-01T00:00:00+00:00',"
            " '2026-10-01T00:00:00+00:00', 2, 1.5)"
        )
        conn.commit()
        db = LessonDB(conn, "repo")
        assert not db.graph_ready()
        apply_migrations(conn)
        assert db.conn.execute("PRAGMA user_version").fetchone()[0] == 22
        assert db.graph_ready()
        # Backfilled: the old lesson is searchable and carries its `applies-to` edge.
        assert [lesson.id for lesson in db.graph_query("p", "batch api")] == ["L1"]
        edges = [(e.src, e.edge, e.dst, e.origin) for e in db.lesson_edges("p")]
        assert ("L1", "applies-to", "src/api/batch.py", "backfill") in edges
        # And graph writes work for repo profiles now.
        assert db.add_edge("p", "L1", "applies-to", "src/auth/")
    finally:
        conn.close()


def test_fts_stays_in_sync(shared_db: LessonDB) -> None:
    lesson = shared_db.add_lesson("work", "Retry flaky tests with tenacity", ["coder"], ["t"])
    assert [
        row[0]
        for row in shared_db.conn.execute(
            "SELECT rowid FROM lessons_fts WHERE lessons_fts MATCH 'tenacity'"
        )
    ] == [lesson.number]
    shared_db.update_text("work", lesson.number, "Something else entirely")
    assert (
        shared_db.conn.execute(
            "SELECT COUNT(*) FROM lessons_fts WHERE lessons_fts MATCH 'tenacity'"
        ).fetchone()[0]
        == 0
    )
    assert [
        row[0]
        for row in shared_db.conn.execute(
            "SELECT rowid FROM lessons_fts WHERE lessons_fts MATCH 'entirely'"
        )
    ] == [lesson.number]
    shared_db.flush("work")
    assert shared_db.conn.execute("SELECT COUNT(*) FROM lessons_fts").fetchone()[0] == 0


# ---- module helpers -----------------------------------------------------------------------------


def test_module_path_helpers() -> None:
    assert normalize_module_path("src/auth/login.py") == "src/auth/login.py"
    assert normalize_module_path("  SRC/Auth/ ") == "src/auth"
    assert normalize_module_path("./x/y") == "x/y"
    assert normalize_module_path("flaky") is None
    assert normalize_module_path("") is None
    assert qualify_module("src/a", "baton") == "baton:src/a"
    assert qualify_module("src/a", None) == "src/a"
    assert split_module_ref("baton:src/auth") == ("baton", "src/auth")
    assert split_module_ref("src/auth") == (None, "src/auth")
    assert repo_slug_for("/Users/gane/code/baton") == "baton"
    assert repo_slug_for("/x/My Cool Repo") == "my-cool-repo"
    assert repo_slug_for("/") == "repo"


def test_add_lesson_derives_applies_to(shared_db: LessonDB) -> None:
    lesson = shared_db.add_lesson(
        "work", "Cache auth tokens", ["coder"], ["src/auth/", "tokens"], repo_slug="baton"
    )
    edges = [(e.src, e.edge, e.dst) for e in shared_db.lesson_edges("work")]
    assert (lesson.id, "applies-to", "baton:src/auth") in edges
    # "tokens" is not path-looking: no edge.
    assert not [e for e in edges if e[2] == "tokens"]
    # Unqualified when no slug is given.
    other = shared_db.add_lesson("work", "More", ["coder"], ["src/other/x.py"])
    assert (other.id, "applies-to", "src/other/x.py") in [
        (e.src, e.edge, e.dst) for e in shared_db.lesson_edges("work")
    ]


# ---- edge CRUD ----------------------------------------------------------------------------------


def test_edge_crud(shared_db: LessonDB) -> None:
    a = shared_db.add_lesson("work", "Batch API calls", ["coder"], [])
    b = shared_db.add_lesson("work", "Never batch API calls", ["coder"], [])

    assert shared_db.add_edge("work", b.id, "supersedes", a.id)
    assert not shared_db.add_edge("work", b.id, "supersedes", a.id)  # duplicate
    assert shared_db.add_edge("work", a.id, "contradicts", b.id, origin="learner")

    views = {(v.other, v.direction, v.edge) for v in shared_db.edges_for_lesson("work", a.number)}
    assert (b.id, "in", "supersedes") in views
    # contradicts is symmetric in display: one stored row shows on both sides.
    assert (b.id, "out", "contradicts") in {
        (v.other, v.direction, v.edge) for v in shared_db.edges_for_lesson("work", a.number)
    } | {(v.other, v.direction, v.edge) for v in shared_db.edges_for_lesson("work", b.number)}

    with pytest.raises(LearningError):
        shared_db.add_edge("work", a.id, "similar-to", b.id)
    with pytest.raises(LearningError):
        shared_db.add_edge("work", a.id, "supersedes", a.id)
    with pytest.raises(LearningError):
        shared_db.add_edge("work", a.id, "contradicts", "S999")
    with pytest.raises(LearningError):
        shared_db.add_edge("work", "L1", "contradicts", b.id)  # wrong scope prefix

    # contradicts unlinks in either direction.
    assert shared_db.remove_edge("work", b.id, "contradicts", a.id)
    assert not shared_db.remove_edge("work", a.id, "contradicts", b.id)
    assert shared_db.remove_edge("work", b.id, "supersedes", a.id)


def test_supersede_is_advisory_only_no_auto_retire(shared_db: LessonDB) -> None:
    old = shared_db.add_lesson("work", "Old lesson", ["coder"], [])
    new = shared_db.add_lesson("work", "New lesson", ["coder"], [])
    assert shared_db.add_edge("work", new.id, "supersedes", old.id)
    old_lesson = shared_db.get_lesson(old.number)
    assert old_lesson is not None
    assert not old_lesson.retired  # still active
    assert old.id in [lesson.id for lesson in shared_db.list_lessons("work")]
    kinds = {(v.direction, v.edge) for v in shared_db.edges_for_lesson("work", old.number)}
    assert ("in", "supersedes") in kinds  # the lineage is visible


def test_edges_for_lesson_includes_derived_from(shared_db: LessonDB) -> None:
    lesson = shared_db.add_lesson("work", "From a run", ["coder"], [], source_run="r42")
    derived = [
        v for v in shared_db.edges_for_lesson("work", lesson.number) if v.edge == "derived-from"
    ]
    assert [(v.direction, v.other_type, v.other, v.origin) for v in derived] == [
        ("out", "run", "r42", "derived")
    ]


def test_flush_and_delete_clean_graph_rows(shared_db: LessonDB) -> None:
    a = shared_db.add_lesson("work", "A", ["coder"], ["src/a.py"])
    b = shared_db.add_lesson("work", "B", ["coder"], [])
    shared_db.add_edge("work", b.id, "contradicts", a.id)
    shared_db.flush("work")
    assert shared_db.lesson_edges("work") == []
    assert shared_db.conn.execute("SELECT COUNT(*) FROM lesson_modules").fetchone()[0] == 0
    assert shared_db.conn.execute("SELECT COUNT(*) FROM lessons_fts").fetchone()[0] == 0
    c = shared_db.add_lesson("work", "C", ["coder"], ["src/c.py"])
    assert c.id == "S3"  # ids are never reused, even across a flush
    shared_db.delete_profile("work")
    assert shared_db.conn.execute("SELECT COUNT(*) FROM lesson_edges").fetchone()[0] == 0


def test_graph_not_ready_on_old_database(tmp_path: Path) -> None:
    """Repo databases before their graph migration degrade gracefully."""
    path = tmp_path / "state.db"
    conn = sqlite3.connect(path)
    conn.executescript(SHARED_MIGRATIONS[0] + "\nPRAGMA user_version = 1;")
    conn.commit()
    db = LessonDB(conn, "repo")
    try:
        assert not db.graph_ready()
        with pytest.raises(LearningError, match="not available"):
            db.add_edge("p", "L1", "contradicts", "L2")
        # selection suppression is a no-op without the tables.
        kept, suppressed = suppress_contradictions(db, conn, "p", [])
        assert (kept, suppressed) == ([], [])
    finally:
        conn.close()


# ---- query --------------------------------------------------------------------------------------


@pytest.fixture
def graphed(shared_db: LessonDB) -> LessonDB:
    s1 = shared_db.add_lesson(
        "work", "Retry flaky HTTP calls in tests with tenacity", ["coder"], ["src/api"], "r1"
    )
    s2 = shared_db.add_lesson(
        "work", "Mark network tests as integration tests", ["coder"], ["tests/test_net.py"], "r2"
    )
    shared_db.add_lesson(
        "work", "Batch API calls to stay under rate limits", ["coder"], ["src/api/batch.py"], "r1"
    )
    shared_db.add_edge("work", s1.id, "contradicts", s2.id)
    return shared_db


def test_graph_query_fts_and_filters(graphed: LessonDB) -> None:
    ids = [lesson.id for lesson in graphed.graph_query("work", "flaky tests")]
    assert ids == ["S1"]  # all quoted terms must match
    assert [lesson.id for lesson in graphed.graph_query("work", "api")] == ["S3", "S1"]
    by_module = [lesson.id for lesson in graphed.graph_query("work", "api", module="src/api")]
    assert by_module == ["S3", "S1"]  # prefix match: src/api/batch.py lessons included
    only_file = [
        lesson.id for lesson in graphed.graph_query("work", "batch", module="src/api/batch.py")
    ]
    assert only_file == ["S3"]
    limited = graphed.graph_query("work", "api", limit=1)
    assert len(limited) == 1
    with pytest.raises(LearningError):
        graphed.graph_query("work", "!!!")  # nothing searchable
    with pytest.raises(LearningError):
        graphed.graph_query("work", "api", since="yesterday")
    with pytest.raises(LearningError):
        graphed.graph_query("work", "api", run_state="failed")  # shared DB holds no runs


def test_graph_query_ranked_by_score_then_newest(graphed: LessonDB) -> None:
    graphed.record_use("work", 1, 5.0)  # S1 wins on score
    assert [lesson.id for lesson in graphed.graph_query("work", "api")] == ["S1", "S3"]


def test_graph_stats(graphed: LessonDB) -> None:
    stats = graphed.graph_stats("work")
    assert (stats.lessons, stats.retired) == (3, 0)
    assert stats.edges["applies-to"] == 3
    assert stats.edges["contradicts"] == 1
    assert stats.edges["derived-from"] == 3
    assert stats.contradictions_open == 1
    graphed.retire("work", 2)
    assert graphed.graph_stats("work").contradictions_open == 0  # one side retired
    assert graphed.graph_stats("work").retired == 1


def test_co_occurs_computed_on_demand(tmp_path: Path, graphed: LessonDB) -> None:
    attempts = sqlite3.connect(tmp_path / "a.db")
    attempts.execute("CREATE TABLE attempts (lesson_ids TEXT)")
    attempts.execute("INSERT INTO attempts VALUES (?)", ('["S1", "S2"]',))
    attempts.execute("INSERT INTO attempts VALUES (?)", ('["S1", "S2"]',))
    attempts.execute("INSERT INTO attempts VALUES (?)", ('["S1", "S3"]',))
    assert graphed.co_occurs(attempts, "work", 1) == [("S2", 2)]
    assert graphed.co_occurs(attempts, "work", 1, min_times=3) == []
    # Nothing is materialized.
    assert "co-occurs-with" not in [
        row[0] for row in attempts.execute("SELECT name FROM sqlite_master")
    ]
    assert (
        graphed.conn.execute(
            "SELECT COUNT(*) FROM lesson_edges WHERE edge = 'co-occurs-with'"
        ).fetchone()[0]
        == 0
    )


# ---- the contradiction rule ---------------------------------------------------------------------


def _attempts_with(*lesson_id_lists: list[str]) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE attempts (lesson_ids TEXT)")
    for ids in lesson_id_lists:
        conn.execute("INSERT INTO attempts VALUES (?)", ('["' + '", "'.join(ids) + '"]',))
    return conn


def test_suppression_needs_co_injection_evidence(graphed: LessonDB) -> None:
    graphed.record_use("work", 1, 2.0)  # S1 outscores S2
    chosen = list(reversed(graphed.list_lessons("work")))  # S3, S2, S1
    # Asserted but never observed together: both stay.
    kept, suppressed = suppress_contradictions(graphed, _attempts_with(["S1"]), "work", chosen)
    assert [lesson.id for lesson in kept] == ["S3", "S2", "S1"] and suppressed == []
    # Observed together before: the lower-scored side goes.
    kept, suppressed = suppress_contradictions(
        graphed, _attempts_with(["S1", "S2"]), "work", chosen
    )
    assert [lesson.id for lesson in kept] == ["S3", "S1"]
    assert [(s.lesson, s.kept) for s in suppressed] == [("S2", "S1")]


def test_suppression_tie_keeps_older(graphed: LessonDB) -> None:
    chosen = graphed.list_lessons("work")  # S1, S2, S3; S1/S2 contradict, scores tied at 0
    kept, suppressed = suppress_contradictions(
        graphed, _attempts_with(["S1", "S2"]), "work", chosen
    )
    assert [lesson.id for lesson in kept] == ["S1", "S3"]
    assert [(s.lesson, s.kept) for s in suppressed] == [("S2", "S1")]


def test_suppression_ignores_pairs_outside_selection(graphed: LessonDB) -> None:
    chosen = [lesson for lesson in graphed.list_lessons("work") if lesson.id == "S1"]
    kept, suppressed = suppress_contradictions(
        graphed, _attempts_with(["S1", "S2"]), "work", chosen
    )
    assert [lesson.id for lesson in kept] == ["S1"] and suppressed == []


def test_co_injected() -> None:
    conn = _attempts_with(["L1", "L2"], ["L3"])
    assert co_injected(conn, "L1", "L2")
    assert not co_injected(conn, "L1", "L3")


def test_select_lessons_itself_unchanged(graphed: LessonDB) -> None:
    """`select_lessons` keeps its ranking; suppression is a separate, explicit step."""
    lessons = graphed.list_lessons("work")
    picked = select_lessons(lessons, "coder", {"api"}, max_lessons=10, max_chars=10_000)
    # No tag matches a single word here, so the newest go first: the graph changes nothing.
    assert [lesson.id for lesson in picked] == ["S3", "S2", "S1"]


# ---- the learner's declarations -----------------------------------------------------------------


def test_learner_declares_edges(graphed: LessonDB) -> None:
    shown = graphed.list_lessons("work")
    out = LearnerOutput(
        add=[
            LessonDraft(
                text="Retry reads, never writes, with tenacity",
                roles=["coder"],
                tags=["src/api"],
                supersedes=["S1"],
                contradicts=["S999"],  # unknown: skipped
                applies_to=["src/api/client.py"],
            )
        ],
        update=[],
        retire=[],
    )
    applied = apply_learner_output(graphed, "work", out, shown, "r9", repo_slug="baton")
    assert "graph edge(s)" in applied.describe()
    edges = {(e.src, e.edge, e.dst, e.origin) for e in graphed.lesson_edges("work")}
    assert ("S4", "supersedes", "S1", "learner") in edges
    assert ("S4", "applies-to", "baton:src/api/client.py", "learner") in edges
    assert ("S4", "applies-to", "baton:src/api", "learner") in edges  # from the path tag
    s1 = graphed.get_lesson(1)
    assert s1 is not None
    assert not s1.retired  # advisory: S1 stays active


def test_learner_edges_bounded_by_max_edges_per_run(graphed: LessonDB) -> None:
    shown = graphed.list_lessons("work")
    drafts = [
        LessonDraft(
            text=f"lesson {i} about retries",
            roles=["coder"],
            tags=[],
            applies_to=[f"src/m{i}/a.py", f"src/m{i}/b.py"],
        )
        for i in range(5)
    ]
    applied = apply_learner_output(
        graphed, "work", LearnerOutput(add=drafts, update=[], retire=[]), shown, "r9"
    )
    assert len(applied.edges) == MAX_EDGES_PER_RUN == 8


def test_learner_edge_declarations_need_no_graph_tables(tmp_path: Path) -> None:
    """On a database without the graph tables the learner's other changes still land."""
    conn = sqlite3.connect(tmp_path / "s.db")
    conn.row_factory = sqlite3.Row
    conn.executescript(SHARED_MIGRATIONS[0] + "\nPRAGMA user_version = 1;")
    db = LessonDB(conn, "shared")
    db.create_profile("p")
    db.add_lesson("p", "Old", ["coder"], [])
    shown = db.list_lessons("p")
    out = LearnerOutput(
        add=[LessonDraft(text="New", roles=["coder"], tags=[], supersedes=["S1"])],
        update=[],
        retire=[],
    )
    applied = apply_learner_output(db, "p", out, shown, "r1")
    assert applied.added == ["S2"] and applied.edges == ()


# ---- CLI round-trips ----------------------------------------------------------------------------


def _shared_profile(repo: Path, name: str = "team") -> None:
    code, out = _jev(repo, "learning", "create", name, "--shared")
    assert code == 0, out


def _add_shared_lessons(repo: Path, name: str = "team") -> tuple[str, str]:
    from jev_agent.core.learning import shared_db_path

    db = LessonDB.open_shared(shared_db_path())
    try:
        a = db.add_lesson(name, "Batch API calls to stay fast", ["coder"], ["src/api"])
        b = db.add_lesson(name, "Never batch API calls, it hits rate limits", ["coder"], [])
        return a.id, b.id
    finally:
        db.conn.close()


def test_graph_cli_round_trip(repo: Path) -> None:
    _shared_profile(repo)
    a, b = _add_shared_lessons(repo)
    assert (a, b) == ("S1", "S2")

    code, out = _jev(repo, "learning", "graph", "link", b, "supersedes", a)
    assert code == 0 and f"Recorded: {b} supersedes {a}" in out, out
    assert "stays active" in out  # the advisory note
    code, out = _jev(repo, "learning", "graph", "link", b, "supersedes", a)
    assert code != 0 and "already recorded" in out

    code, out = _jev(repo, "learning", "graph", "show", b)
    assert code == 0 and "supersedes" in out and a in out, out

    code, out = _jev(repo, "learning", "graph", "link", a, "contradicts", b)
    assert code == 0, out
    code, out = _jev(repo, "learning", "graph", "query", "batch api", "--profile", "team")
    assert code == 0 and a in out and "contradicts" in out, out
    code, out = _jev(
        repo, "learning", "graph", "query", "batch", "--module", "src/api", "--profile", "team"
    )
    assert code == 0 and a in out, out

    code, out = _jev(repo, "learning", "graph", "stats", "--profile", "team")
    assert code == 0 and "lessons 2" in out and "contradicts 1" in out, out

    code, out = _jev(repo, "learning", "graph", "unlink", a, "contradicts", b)
    assert code == 0 and "Removed" in out, out
    code, out = _jev(repo, "learning", "graph", "unlink", a, "contradicts", b)
    assert code == 0 and "No such edge" in out, out

    # superseding never retired the old lesson.
    code, out = _jev(repo, "learning", "show", "team")
    assert code == 0 and a in out, out


def test_graph_link_validates(repo: Path) -> None:
    _shared_profile(repo)
    _add_shared_lessons(repo)
    code, out = _jev(repo, "learning", "graph", "link", "S1", "befriends", "S2")
    assert code != 0 and "unknown edge" in out
    code, out = _jev(repo, "learning", "graph", "link", "S1", "contradicts", "S9")
    assert code != 0 and "no lesson" in out
    code, out = _jev(repo, "learning", "graph", "link", "S1", "applies-to", "src/auth/")
    assert code == 0 and "Recorded" in out, out
    code, out = _jev(
        repo, "learning", "graph", "query", "batch", "--module", "src", "--profile", "team"
    )
    assert code == 0 and "S1" in out, out


def test_graph_query_from_runs_needs_repo_profile(repo: Path, tmp_path: Path) -> None:
    _shared_profile(repo)
    _add_shared_lessons(repo)
    code, out = _jev(
        repo, "learning", "graph", "query", "batch", "--from-runs", "failed", "--profile", "team"
    )
    assert code != 0 and "repo-scope" in out


def test_app_layer_wrappers(repo: Path) -> None:
    _shared_profile(repo)
    a, b = _add_shared_lessons(repo)
    with Store.open(repo / ".jev" / "state.db") as store:
        learning = Learning.for_store(store)
        try:
            assert [lesson.id for lesson in graph_query(learning, "team", "batch api")] == [b, a]
            edge = graph_link(learning, b, "contradicts", a)
            assert (edge.src, edge.edge, edge.dst) == (b, "contradicts", a)
            view = graph_show(learning, b, store.connection)
            assert view.lesson.id == b
            assert any(v.edge == "contradicts" for v in view.edges)
            stats = graph_stats(learning, "team")
            assert stats.edges["contradicts"] == 1 and stats.contradictions_open == 1
            assert graph_unlink(learning, b, "contradicts", a)
            assert graph_stats(learning, "team").contradictions_open == 0
        finally:
            learning.close()


# ---- dashboard ----------------------------------------------------------------------------------


def _dashboard_client(repo: Path) -> TestClient:
    return TestClient(
        create_app(DashboardSettings(root=repo, allowed_hosts=frozenset({"testserver"})))
    )


def test_dashboard_graph_tab(repo: Path) -> None:
    _shared_profile(repo)
    a, b = _add_shared_lessons(repo)
    from jev_agent.core.learning import shared_db_path

    db = LessonDB.open_shared(shared_db_path())
    try:
        db.add_edge("team", b, "contradicts", a)
    finally:
        db.conn.close()
    (repo / "jev.toml").write_text(CONFIG + '[learning]\nenabled = true\nprofile = "team"\n')
    with _dashboard_client(repo) as client:
        page = client.get("/learning", params={"profile": "team", "tab": "graph"})
        assert page.status_code == 200, page.text[:500]
        assert "Graph" in page.text and a in page.text and "contradictions" in page.text
        # The list tab still renders as before.
        listed = client.get("/learning", params={"profile": "team"})
        assert listed.status_code == 200 and "Lessons in team" in listed.text
        # Facet filtering narrows the rows.
        filtered = client.get(
            "/learning", params={"profile": "team", "tab": "graph", "rel": "contradictions"}
        )
        assert filtered.status_code == 200 and b in filtered.text
        # Focusing a lesson renders its neighborhood.
        focused = client.get("/learning", params={"profile": "team", "tab": "graph", "lesson": b})
        assert focused.status_code == 200
        assert "One-hop neighborhood" in focused.text and "contradicts" in focused.text
        # Unknown lesson id: no crash, no neighborhood.
        missing = client.get(
            "/learning", params={"profile": "team", "tab": "graph", "lesson": "S99"}
        )
        assert missing.status_code == 200 and "One-hop neighborhood" not in missing.text


def test_dashboard_graph_tab_repo_profile(repo: Path) -> None:
    """A repo-scope profile renders the Graph tab (issue #319): no silent fallback
    to List — the Graph button is the highlighted one and the pages differ."""
    code, out = _jev(repo, "learning", "create", "local")
    assert code == 0, out
    with _dashboard_client(repo) as client:
        page = client.get("/learning", params={"profile": "local", "tab": "graph"})
        assert page.status_code == 200, page.text[:500]
        assert "0 lessons" in page.text and "Search lessons" in page.text
        assert "Link lessons" in page.text and "No lessons match" in page.text
        assert (
            '<a class="btn btn-sm btn-primary"'
            ' href="/learning?profile=local&amp;tab=graph">Graph</a>' in page.text
        )
        listed = client.get("/learning", params={"profile": "local"})
        assert listed.status_code == 200 and page.text != listed.text


def test_graph_cli_round_trip_repo_profile(repo: Path) -> None:
    """The `graph` CLI works for repo-scope profiles too (issue #319)."""
    code, out = _jev(repo, "learning", "create", "local")
    assert code == 0, out
    with Store.open(repo / ".jev" / "state.db") as store:
        learning = Learning.for_store(store)
        try:
            a = learning.repo.add_lesson(
                "local", "Batch API calls to stay fast", ["coder"], ["src/api"]
            )
            b = learning.repo.add_lesson("local", "Never batch API calls", ["coder"], [])
        finally:
            learning.close()
    assert (a.id, b.id) == ("L1", "L2")

    code, out = _jev(repo, "learning", "graph", "link", b.id, "contradicts", a.id)
    assert code == 0 and f"Recorded: {b.id} contradicts {a.id}" in out, out
    code, out = _jev(repo, "learning", "graph", "query", "batch", "--profile", "local")
    assert code == 0 and a.id in out and "contradicts" in out, out
    code, out = _jev(repo, "learning", "graph", "stats", "--profile", "local")
    assert code == 0 and "lessons 2" in out and "contradicts 1" in out, out
