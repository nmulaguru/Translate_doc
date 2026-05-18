PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
PRAGMA foreign_keys=ON;
PRAGMA cache_size=-32768;   -- 32 MB page cache (negative = KB units)
PRAGMA mmap_size=134217728; -- 128 MB memory-mapped I/O for read-heavy workloads
PRAGMA temp_store=MEMORY;   -- temp tables/indexes stay in RAM, not on disk

CREATE TABLE IF NOT EXISTS sessions (
    id            TEXT PRIMARY KEY,
    container_id  TEXT,
    status        TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    user_msg      TEXT NOT NULL,
    final_answer  TEXT
);

CREATE TABLE IF NOT EXISTS plans (
    id          TEXT PRIMARY KEY,
    session_id  TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    goal        TEXT NOT NULL,
    json_blob   TEXT NOT NULL,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tasks (
    id              TEXT PRIMARY KEY,
    plan_id         TEXT NOT NULL REFERENCES plans(id) ON DELETE CASCADE,
    kind            TEXT NOT NULL,
    title           TEXT NOT NULL,
    depends_on_json TEXT NOT NULL,
    spec_json       TEXT NOT NULL,
    status          TEXT NOT NULL,
    attempts        INTEGER NOT NULL DEFAULT 0,
    output_blob     TEXT,
    artifact_ref    TEXT,
    error           TEXT,
    started_at      TEXT,
    ended_at        TEXT
);

CREATE TABLE IF NOT EXISTS events (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id   TEXT NOT NULL,
    ts           TEXT NOT NULL,
    type         TEXT NOT NULL,
    payload_json TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_events_session ON events(session_id, id);
-- Enables fast "find all events of type X for session Y" without full-table scan
CREATE INDEX IF NOT EXISTS idx_events_session_type ON events(session_id, type);

-- Enables fast "find all PENDING/FAILED tasks in a plan" used by scheduler + replan
CREATE INDEX IF NOT EXISTS idx_tasks_plan_status ON tasks(plan_id, status);

-- Plans by session — used when resuming sessions or replanning
CREATE INDEX IF NOT EXISTS idx_plans_session ON plans(session_id);

CREATE TABLE IF NOT EXISTS questions (
    id           TEXT PRIMARY KEY,
    session_id   TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    text         TEXT NOT NULL,
    options_json TEXT,
    answer       TEXT,
    asked_at     TEXT NOT NULL,
    answered_at  TEXT
);

CREATE TABLE IF NOT EXISTS checkpoints (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id   TEXT NOT NULL,
    task_id      TEXT NOT NULL,
    output_ref   TEXT NOT NULL,
    created_at   TEXT NOT NULL
);
