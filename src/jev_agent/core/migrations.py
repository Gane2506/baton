"""SQLite schema migrations for the state store (PLAN.md section 9).

Each entry in `MIGRATIONS` is one SQL script. Script N (1-based) moves the database from
`PRAGMA user_version` N-1 to N. Never edit a released script: append a new one instead.
"""

from __future__ import annotations

import sqlite3
import time

# Profiles and lessons for recursive learning (docs/learning.md). The same tables live in the
# repo's state.db (migration 4) and in the shared learning database (`SHARED_MIGRATIONS`).
# Like every released script, never edit this text: add a new migration instead.
LEARNING_TABLES_V1 = """
    CREATE TABLE learning_profiles (
        name        TEXT PRIMARY KEY,
        enabled     INTEGER NOT NULL DEFAULT 1,
        created_at  TEXT NOT NULL
    );

    -- source_run/source_task have no foreign key: shared databases hold no runs.
    CREATE TABLE lessons (
        id           INTEGER PRIMARY KEY,
        profile      TEXT NOT NULL REFERENCES learning_profiles (name) ON DELETE CASCADE,
        text         TEXT NOT NULL,
        roles_json   TEXT NOT NULL,
        tags_json    TEXT NOT NULL,
        source_run   TEXT,
        source_task  TEXT,
        created_at   TEXT NOT NULL,
        updated_at   TEXT NOT NULL,
        uses         INTEGER NOT NULL DEFAULT 0,
        score        REAL NOT NULL DEFAULT 0,
        retired      INTEGER NOT NULL DEFAULT 0
    );

    CREATE INDEX lessons_profile ON lessons (profile);
"""

# Issue #40: rebuild `lessons` with AUTOINCREMENT so an id is never handed out again after its
# lesson was deleted (flush, delete), and add the ledger of scored (run, task) pairs, kept in the
# database that holds the scores so scoring and its record commit together. Ids and data are
# kept; a lesson whose profile is missing (only possible with foreign keys off) is dropped.
# Used by migration 9 (state.db) and shared migration 2. Never edit this text.
LESSONS_AUTOINCREMENT_V2 = """
    CREATE TABLE lessons_v2 (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        profile      TEXT NOT NULL REFERENCES learning_profiles (name) ON DELETE CASCADE,
        text         TEXT NOT NULL,
        roles_json   TEXT NOT NULL,
        tags_json    TEXT NOT NULL,
        source_run   TEXT,
        source_task  TEXT,
        created_at   TEXT NOT NULL,
        updated_at   TEXT NOT NULL,
        uses         INTEGER NOT NULL DEFAULT 0,
        score        REAL NOT NULL DEFAULT 0,
        retired      INTEGER NOT NULL DEFAULT 0
    );

    INSERT INTO lessons_v2 (id, profile, text, roles_json, tags_json, source_run, source_task,
                            created_at, updated_at, uses, score, retired)
        SELECT id, profile, text, roles_json, tags_json, source_run, source_task, created_at,
               updated_at, uses, score, retired
        FROM lessons WHERE profile IN (SELECT name FROM learning_profiles);

    DROP TABLE lessons;
    ALTER TABLE lessons_v2 RENAME TO lessons;
    CREATE INDEX lessons_profile ON lessons (profile);

    -- One row per task whose injected lessons were scored (docs/learning.md, scoring).
    CREATE TABLE scored_tasks (
        run_id     TEXT NOT NULL,
        task_id    TEXT NOT NULL,
        profile    TEXT NOT NULL,
        scored_at  TEXT NOT NULL,
        PRIMARY KEY (run_id, task_id)
    );
"""

# Issue #69: a learning profile can carry tuned Laya matrix weights (`jev laya tune --apply`);
# NULL means the configured weights. Used by migration 13 (state.db) and shared migration 3.
# Never edit this text.
PROFILE_LAYA_WEIGHTS = """
    ALTER TABLE learning_profiles ADD COLUMN laya_weights_json TEXT;
"""

MIGRATIONS: list[str] = [
    # 1: initial schema
    """
    CREATE TABLE runs (
        id          TEXT PRIMARY KEY,
        idea_json   TEXT NOT NULL,
        state       TEXT NOT NULL,
        plan_json   TEXT,
        plan_round  INTEGER NOT NULL DEFAULT 0,
        spend_usd   REAL NOT NULL DEFAULT 0,
        created_at  TEXT NOT NULL,
        updated_at  TEXT NOT NULL
    );

    CREATE TABLE tasks (
        run_id     TEXT NOT NULL REFERENCES runs (id),
        id         TEXT NOT NULL,
        position   INTEGER NOT NULL,  -- index of the card in the plan
        card_json  TEXT NOT NULL,
        state      TEXT NOT NULL,
        attempts   INTEGER NOT NULL DEFAULT 0,
        spend_usd  REAL NOT NULL DEFAULT 0,
        PRIMARY KEY (run_id, id)
    );

    -- task_id is NULL for run-level work such as planning and validation.
    CREATE TABLE attempts (
        id           INTEGER PRIMARY KEY,
        run_id       TEXT NOT NULL REFERENCES runs (id),
        task_id      TEXT,
        attempt_no   INTEGER NOT NULL,
        role         TEXT NOT NULL,
        worker       TEXT NOT NULL,
        model        TEXT,
        effort       TEXT,
        status       TEXT,
        result_json  TEXT,
        cost_usd     REAL,
        log_path     TEXT,
        started_at   TEXT NOT NULL,
        finished_at  TEXT,
        FOREIGN KEY (run_id, task_id) REFERENCES tasks (run_id, id)
    );

    -- point/dispatcher/model/cost_usd/latency_ms duplicate decision_json for easy querying.
    CREATE TABLE decisions (
        id               INTEGER PRIMARY KEY,
        run_id           TEXT NOT NULL REFERENCES runs (id),
        task_id          TEXT,
        point            TEXT NOT NULL,
        request_json     TEXT NOT NULL,
        decision_json    TEXT NOT NULL,
        dispatcher       TEXT NOT NULL,
        model            TEXT,
        cost_usd         REAL,
        latency_ms       INTEGER NOT NULL,
        fallback_reason  TEXT,
        created_at       TEXT NOT NULL,
        FOREIGN KEY (run_id, task_id) REFERENCES tasks (run_id, id)
    );

    CREATE INDEX tasks_run_id ON tasks (run_id);
    CREATE INDEX attempts_run_id ON attempts (run_id);
    CREATE INDEX decisions_run_id ON decisions (run_id);
    """,
    # 2: pipeline bookkeeping (M2): why a run stopped, the last validation verdict, and each
    # task's current worker assignment plus a note (feedback for the next attempt, or why it
    # failed).
    """
    ALTER TABLE runs ADD COLUMN reason TEXT;
    ALTER TABLE runs ADD COLUMN validation_json TEXT;
    ALTER TABLE tasks ADD COLUMN worker TEXT;
    ALTER TABLE tasks ADD COLUMN model TEXT;
    ALTER TABLE tasks ADD COLUMN effort TEXT;
    ALTER TABLE tasks ADD COLUMN note TEXT;
    """,
    # 3: the state a NEEDS_HUMAN run returns to on `resume` once the human has fixed the
    # problem (for example a broken prompt template). NULL means the stop is final.
    """
    ALTER TABLE runs ADD COLUMN resume_state TEXT;
    """,
    # 4: recursive learning. Repo settings set by `jev learning` (active profile, on/off), the
    # profile each run uses (NULL: learning off) and its learning outcome, lesson ids injected
    # into each attempt (JSON list), whether a task's lessons were scored, and the repo-scope
    # profile and lesson tables.
    """
    CREATE TABLE settings (
        key    TEXT PRIMARY KEY,
        value  TEXT NOT NULL
    );
    ALTER TABLE runs ADD COLUMN learning_profile TEXT;
    ALTER TABLE runs ADD COLUMN learning_outcome TEXT;
    ALTER TABLE tasks ADD COLUMN lessons_scored INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE attempts ADD COLUMN lesson_ids TEXT;
    """
    + LEARNING_TABLES_V1,
    # 5: the idea queue worked through by `jev scheduler` (docs/queue.md). Ordered by priority,
    # then position. `run_id` links an item to the run started for it; `profile` is a workspace
    # profile name, stored and passed through to the run.
    """
    CREATE TABLE queue (
        id                INTEGER PRIMARY KEY,
        idea              TEXT NOT NULL,
        priority          TEXT NOT NULL DEFAULT 'normal'
                          CHECK (priority IN ('high', 'normal', 'low')),
        position          INTEGER NOT NULL,
        status            TEXT NOT NULL DEFAULT 'queued'
                          CHECK (status IN ('queued', 'running', 'waiting', 'done', 'failed',
                                            'cancelled')),
        run_id            TEXT REFERENCES runs (id),
        plan_only         INTEGER NOT NULL DEFAULT 0,
        learning_profile  TEXT,
        no_learning       INTEGER NOT NULL DEFAULT 0,
        profile           TEXT,
        note              TEXT,
        created_at        TEXT NOT NULL,
        updated_at        TEXT NOT NULL
    );

    CREATE INDEX queue_status ON queue (status);
    """,
    # 6: the quality gate (docs/quality.md). The level each run uses (NULL: "none", as for runs
    # made before this migration), the level a queue item asks for (NULL: the config's), and
    # one row per gate (a task attempt or an integration round) with its result as JSON.
    """
    ALTER TABLE runs ADD COLUMN quality_level TEXT;
    ALTER TABLE queue ADD COLUMN quality TEXT;
    CREATE TABLE quality_reports (
        id           INTEGER PRIMARY KEY,
        run_id       TEXT NOT NULL REFERENCES runs (id),
        task_id      TEXT,
        scope        TEXT NOT NULL CHECK (scope IN ('task', 'integration')),
        number       INTEGER NOT NULL,
        level        TEXT NOT NULL,
        blocked      INTEGER NOT NULL,
        result_json  TEXT NOT NULL,
        report_path  TEXT,
        created_at   TEXT NOT NULL
    );

    CREATE INDEX quality_reports_run ON quality_reports (run_id);
    """,
    # 7: `jev ship` (docs/ship.md). One row per pull request opened for a run, with what
    # `jev ship sync` last read from GitHub; `pushed_commits` is how many commits the pushed
    # branch had over its base (for "commits after open"). `run_kpis` keeps the run's KPIs as
    # computed when it was shipped, so they stay stable when logs or branches go away.
    """
    CREATE TABLE shipped_prs (
        id                  INTEGER PRIMARY KEY,
        run_id              TEXT NOT NULL REFERENCES runs (id),
        remote              TEXT NOT NULL,
        repo                TEXT NOT NULL,
        branch              TEXT NOT NULL,
        base                TEXT NOT NULL,
        number              INTEGER,
        url                 TEXT,
        title               TEXT NOT NULL DEFAULT '',
        draft               INTEGER NOT NULL DEFAULT 0,
        head_oid            TEXT,
        pushed_commits      INTEGER,
        created_at          TEXT NOT NULL,
        state               TEXT NOT NULL DEFAULT 'open'
                            CHECK (state IN ('open', 'closed', 'merged')),
        merged_at           TEXT,
        closed_at           TEXT,
        review_comments     INTEGER,
        reviews             INTEGER,
        ci_status           TEXT,
        commits_after_open  INTEGER,
        additions           INTEGER,
        deletions           INTEGER,
        changed_files       INTEGER,
        reverted            INTEGER,
        last_synced_at      TEXT,
        raw_json            TEXT
    );

    CREATE INDEX shipped_prs_run ON shipped_prs (run_id);

    CREATE TABLE run_kpis (
        run_id       TEXT PRIMARY KEY REFERENCES runs (id),
        computed_at  TEXT NOT NULL,
        kpis_json    TEXT NOT NULL
    );
    """,
    # 8: `--plan-only` is a property of the run, so `jev resume` keeps stopping at the approval
    # gate (issue #22). Older runs: 0 (their flag was never stored).
    """
    ALTER TABLE runs ADD COLUMN plan_only INTEGER NOT NULL DEFAULT 0;
    """,
    # 9: lesson ids are never reused, and scoring is recorded with the scores (issue #40). Ids
    # deleted before this upgrade are unknown, except the repo-scope (`L`) ids that attempts
    # still name: the sequence starts after the highest of those too.
    LESSONS_AUTOINCREMENT_V2
    + """
    DELETE FROM sqlite_sequence WHERE name = 'lessons';
    INSERT INTO sqlite_sequence (name, seq)
        SELECT 'lessons', top FROM (
            SELECT MAX(n) AS top FROM (
                SELECT id AS n FROM lessons
                UNION ALL
                SELECT CAST(substr(j.value, 2) AS INTEGER)
                FROM attempts AS a,
                     json_each(CASE WHEN json_valid(a.lesson_ids) THEN a.lesson_ids
                                    ELSE '[]' END) AS j
                WHERE j.type = 'text' AND upper(substr(j.value, 1, 1)) = 'L'
            )
        )
        WHERE top IS NOT NULL;
    """,
    # 10: what the end-of-run learning steps changed, as data (issue #42): a JSON object
    # {"added", "updated", "retired"} summed over the run's learning steps; NULL: nothing was
    # applied. Older runs are backfilled once from their outcome line, "learned into <profile>:
    # A added, U updated, R retired" (profile names have no ": "); nothing parses it after this.
    """
    ALTER TABLE runs ADD COLUMN learning_counts_json TEXT;
    UPDATE runs SET learning_counts_json = json_object(
            'added', CAST(substr(learning_outcome, instr(learning_outcome, ': ') + 2)
                          AS INTEGER),
            'updated', CAST(substr(learning_outcome, instr(learning_outcome, ' added, ') + 8)
                            AS INTEGER),
            'retired', CAST(substr(learning_outcome, instr(learning_outcome, ' updated, ') + 10)
                            AS INTEGER))
        WHERE learning_outcome GLOB 'learned into *: [0-9]* added, [0-9]* updated, [0-9]* retired';
    """,
    # 11: the run lease (issue #61): at most one process drives a run at a time. `token` is
    # random per claim; `pid` and `host` say who holds it; the holder renews `heartbeat_at`.
    """
    CREATE TABLE run_leases (
        run_id        TEXT PRIMARY KEY REFERENCES runs (id) ON DELETE CASCADE,
        token         TEXT NOT NULL,
        pid           INTEGER NOT NULL,
        host          TEXT NOT NULL,
        acquired_at   TEXT NOT NULL,
        heartbeat_at  TEXT NOT NULL
    );
    """,
    # 12: what happens after both votes approve at D6 (issue #68): `jev run --auto-ship` or
    # `--ask-before-ship` on the run and on a queue item; NULL: `[ship] after_approval`.
    """
    ALTER TABLE runs ADD COLUMN ship_mode TEXT CHECK (ship_mode IN ('merge', 'ask'));
    ALTER TABLE queue ADD COLUMN ship_mode TEXT CHECK (ship_mode IN ('merge', 'ask'));
    """,
    # 13: tuned Laya weights on a learning profile (issue #69).
    PROFILE_LAYA_WEIGHTS,
    # 14: autonomy mode for Phase 2 workstream C: how much of the pipeline runs
    # without a person, recorded per run. Defaults preserve historical behavior.
    """
    ALTER TABLE runs ADD COLUMN autonomy_mode TEXT DEFAULT 'standard'
        CHECK (autonomy_mode IN ('standard', 'autonomous', 'ask_every_step', 'manual', 'medium'));
    ALTER TABLE runs ADD COLUMN autonomous_opt_in INTEGER DEFAULT 0;
    ALTER TABLE runs ADD COLUMN medium_folder TEXT;
    """,
    # 15: recurring tasks: `brief` re-enqueued on the cron `schedule` in `timezone`. The
    # original schedule text lives in `schedule_source` (`schedule` is the normalized
    # cron from `core.recurrence`); `active_item_id` is the in-flight queue item, NULL
    # when the task is idle between runs.
    """
    CREATE TABLE recurring_tasks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        brief TEXT NOT NULL,
        schedule TEXT NOT NULL,
        schedule_source TEXT NOT NULL,
        timezone TEXT NOT NULL,
        next_due_at TEXT NOT NULL,
        last_run_at TEXT,
        enabled INTEGER NOT NULL DEFAULT 1,
        active_item_id INTEGER,
        priority TEXT NOT NULL DEFAULT 'normal',
        plan_only INTEGER NOT NULL DEFAULT 0,
        learning_profile TEXT,
        no_learning INTEGER NOT NULL DEFAULT 0,
        profile TEXT,
        quality TEXT,
        ship_mode TEXT,
        created_at TEXT NOT NULL
    );
    """,
    # 16: the DESIGNING pipeline stage: the designer's `DesignDoc` for the run, as JSON.
    # NULL: the run never needed a design (its plan had `needs_design` false, or the run
    # predates the stage). The initial CREATE TABLE (migration 1) has no such column, so
    # old databases get it here.
    """
    ALTER TABLE runs ADD COLUMN design_json TEXT;
    """,
    # 18: event-sourced execution state (docs/events.md): the append-only `run_events`
    # log every run mutation is dual-written to, plus `run_leases.event_seq` so a
    # driver can record how far its event log has got (used by `jev status` in Phase 3).
    # The single-writer invariant (one lease holder per run; `run_seq = MAX(run_seq)+1`
    # inside the same transaction as the state write) keeps per-run ordering gapless.
    """
    CREATE TABLE run_events (
        seq            INTEGER PRIMARY KEY AUTOINCREMENT,  -- global insertion order
        run_id         TEXT NOT NULL REFERENCES runs (id) ON DELETE CASCADE,
        run_seq        INTEGER NOT NULL,                   -- per-run ordering
        type           TEXT NOT NULL,
        schema_v       INTEGER NOT NULL DEFAULT 1,
        ts             TEXT NOT NULL,
        actor          TEXT NOT NULL,
        parent_run_seq INTEGER,                            -- causal parent in this run
        synthetic      INTEGER NOT NULL DEFAULT 0,
        payload_json   TEXT NOT NULL,
        UNIQUE (run_id, run_seq)
    );
    CREATE INDEX run_events_run ON run_events (run_id, run_seq);
    CREATE INDEX run_events_type ON run_events (run_id, type);
    ALTER TABLE run_leases ADD COLUMN event_seq INTEGER;
    """,
]

SCHEMA_VERSION = len(MIGRATIONS)

# Migrations for the shared learning database (`$XDG_DATA_HOME/jev/learning.db`).
SHARED_MIGRATIONS: list[str] = [
    LEARNING_TABLES_V1,
    # 2: lesson ids are never reused; the scored (run, task) ledger (issue #40).
    LESSONS_AUTOINCREMENT_V2,
    # 3: tuned Laya weights on a learning profile (issue #69).
    PROFILE_LAYA_WEIGHTS,
]


class SchemaVersionError(RuntimeError):
    """The database was written by a newer version of jev than this one."""


def schema_version(conn: sqlite3.Connection) -> int:
    version: int = conn.execute("PRAGMA user_version").fetchone()[0]
    return version


def configure_connection(conn: sqlite3.Connection, busy_timeout_ms: int = 5000) -> None:
    """Set up a fresh connection: foreign keys on, WAL journal, a busy timeout.

    Switching to WAL needs an exclusive lock and SQLite reports a busy database at once,
    without waiting, so the switch is retried for up to the busy timeout: another process may
    be opening (or upgrading) the same database right now (issue #23).
    """
    conn.execute(f"PRAGMA busy_timeout = {int(busy_timeout_ms)}")
    conn.execute("PRAGMA foreign_keys = ON")
    deadline = time.monotonic() + busy_timeout_ms / 1000
    while True:
        try:
            conn.execute("PRAGMA journal_mode = WAL").fetchall()
            return
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc) or time.monotonic() >= deadline:
                raise
            time.sleep(0.02)


def _statements(script: str) -> list[str]:
    """`script` split into complete SQL statements (comments and blank text dropped)."""
    statements: list[str] = []
    pending = ""
    for piece in script.split(";")[:-1]:
        pending += piece + ";"
        # A `;` inside a string, comment or trigger body does not end the statement yet.
        if sqlite3.complete_statement(pending):
            statements.append(pending)
            pending = ""
    pending += script.rsplit(";", 1)[-1] if ";" in script else script
    code = [ln for ln in pending.splitlines() if ln.strip() and not ln.strip().startswith("--")]
    if code:  # a last statement without `;`
        statements.append(pending)
    return statements


def apply_migrations(conn: sqlite3.Connection, migrations: list[str] | None = None) -> None:
    """Bring the database up to the last of `migrations` (default: the state database's
    `MIGRATIONS`, i.e. `SCHEMA_VERSION`). Safe to call on every open, also from several
    processes at once (issue #23).

    Each script runs in its own `BEGIN IMMEDIATE` transaction together with its `user_version`
    bump, so a failure leaves the database at the last fully applied version. The version is
    read again after the write lock is taken: another process may have upgraded the database
    in the meantime, and its scripts are then skipped. `conn` must be in autocommit mode
    (`isolation_level=None`).
    """
    scripts = MIGRATIONS if migrations is None else migrations
    latest = len(scripts)

    def check(current: int) -> int:
        if current > latest:
            raise SchemaVersionError(
                f"database has schema version {current}, but this jev only knows up to "
                f"{latest}; upgrade jev to open it"
            )
        return current

    if check(schema_version(conn)) == latest:
        return  # up to date: no write lock needed
    while True:
        # Not `executescript`: before Python 3.12 it commits an open transaction first.
        conn.execute("BEGIN IMMEDIATE")
        try:
            current = check(schema_version(conn))
            if current == latest:
                conn.execute("COMMIT")
                return
            for statement in _statements(scripts[current]):
                conn.execute(statement)
            conn.execute(f"PRAGMA user_version = {current + 1}")
            conn.execute("COMMIT")
        except BaseException:
            if conn.in_transaction:
                conn.rollback()
            raise
