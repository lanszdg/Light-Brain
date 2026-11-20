PRAGMA user_version=1;
CREATE TABLE IF NOT EXISTS brains (
    id TEXT PRIMARY KEY, revision INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS cases (
    id TEXT PRIMARY KEY, brain TEXT NOT NULL REFERENCES brains(id), project TEXT NOT NULL,
    revision INTEGER NOT NULL, request TEXT NOT NULL, data TEXT NOT NULL, created REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS case_scope ON cases(brain,project);
CREATE TABLE IF NOT EXISTS events (
    brain TEXT NOT NULL, event_id TEXT NOT NULL, case_id TEXT NOT NULL REFERENCES cases(id),
    revision INTEGER NOT NULL, event_type TEXT NOT NULL, body TEXT NOT NULL,
    fingerprint TEXT NOT NULL, created REAL NOT NULL, PRIMARY KEY(brain,event_id),
    UNIQUE(case_id,revision)
);
CREATE TABLE IF NOT EXISTS graph (
    case_id TEXT NOT NULL REFERENCES cases(id), kind TEXT NOT NULL, id TEXT NOT NULL,
    valid_from INTEGER NOT NULL, valid_to INTEGER, body TEXT NOT NULL,
    PRIMARY KEY(case_id,kind,id,valid_from)
);
CREATE UNIQUE INDEX IF NOT EXISTS graph_live ON graph(case_id,kind,id) WHERE valid_to IS NULL;
CREATE TABLE IF NOT EXISTS criteria (
    case_id TEXT NOT NULL REFERENCES cases(id), id TEXT NOT NULL, version INTEGER NOT NULL,
    frozen_at INTEGER NOT NULL, digest TEXT NOT NULL, body TEXT NOT NULL,
    PRIMARY KEY(case_id,id,version)
);
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY, case_id TEXT NOT NULL REFERENCES cases(id), revision INTEGER NOT NULL,
    kind TEXT NOT NULL, payload TEXT NOT NULL, status TEXT NOT NULL,
    attempt INTEGER NOT NULL DEFAULT 0, worker TEXT, lease_until REAL,
    receipt TEXT, consumed_at INTEGER, created REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS job_queue ON jobs(status,lease_until,created);
CREATE INDEX IF NOT EXISTS case_jobs ON jobs(case_id,revision);
CREATE TABLE IF NOT EXISTS artifacts (
    digest TEXT PRIMARY KEY, size INTEGER NOT NULL, created REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS artifact_refs (
    digest TEXT NOT NULL REFERENCES artifacts(digest), case_id TEXT NOT NULL REFERENCES cases(id),
    job_id TEXT NOT NULL, PRIMARY KEY(digest,case_id,job_id)
);
CREATE TABLE IF NOT EXISTS methods (
    brain TEXT NOT NULL REFERENCES brains(id), project TEXT NOT NULL, id TEXT NOT NULL,
    revision INTEGER NOT NULL, status TEXT NOT NULL, body TEXT NOT NULL, created REAL NOT NULL,
    PRIMARY KEY(brain,project,id,revision)
);
