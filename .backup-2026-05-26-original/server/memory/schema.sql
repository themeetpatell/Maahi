-- ═══════════════════════════════════════════════════════════════
-- Maahi memory schema
-- All tables idempotent. Embeddings are Voyage voyage-3-large (1024d, float).
-- Timestamps are ISO 8601 UTC.
-- ═══════════════════════════════════════════════════════════════

-- ─── Facts: declarative knowledge ─────────────────────────────
CREATE TABLE IF NOT EXISTS facts (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  key        TEXT    NOT NULL,
  value      TEXT    NOT NULL,
  category   TEXT    DEFAULT 'general',
  source     TEXT    DEFAULT 'manual',
  created_at TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  updated_at TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_facts_key      ON facts(key);
CREATE INDEX        IF NOT EXISTS idx_facts_category ON facts(category);

CREATE VIRTUAL TABLE IF NOT EXISTS vec_facts USING vec0(
  embedding float[1024]
);

CREATE VIRTUAL TABLE IF NOT EXISTS fts_facts USING fts5(
  key, value, category,
  content='facts', content_rowid='id', tokenize='porter unicode61'
);
CREATE TRIGGER IF NOT EXISTS facts_ai AFTER INSERT ON facts BEGIN
  INSERT INTO fts_facts(rowid, key, value, category)
  VALUES (new.id, new.key, new.value, new.category);
END;
CREATE TRIGGER IF NOT EXISTS facts_ad AFTER DELETE ON facts BEGIN
  INSERT INTO fts_facts(fts_facts, rowid, key, value, category)
  VALUES('delete', old.id, old.key, old.value, old.category);
END;
CREATE TRIGGER IF NOT EXISTS facts_au AFTER UPDATE ON facts BEGIN
  INSERT INTO fts_facts(fts_facts, rowid, key, value, category)
  VALUES('delete', old.id, old.key, old.value, old.category);
  INSERT INTO fts_facts(rowid, key, value, category)
  VALUES (new.id, new.key, new.value, new.category);
END;

-- ─── Episodes: chat turns as autobiographical memory ──────────
CREATE TABLE IF NOT EXISTS episodes (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  conversation_id TEXT,
  role            TEXT NOT NULL,
  content         TEXT NOT NULL,
  mode            TEXT,
  created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE INDEX IF NOT EXISTS idx_episodes_convo   ON episodes(conversation_id);
CREATE INDEX IF NOT EXISTS idx_episodes_created ON episodes(created_at DESC);

CREATE VIRTUAL TABLE IF NOT EXISTS vec_episodes USING vec0(
  embedding float[1024]
);

CREATE VIRTUAL TABLE IF NOT EXISTS fts_episodes USING fts5(
  content,
  content='episodes', content_rowid='id', tokenize='porter unicode61'
);
CREATE TRIGGER IF NOT EXISTS episodes_ai AFTER INSERT ON episodes BEGIN
  INSERT INTO fts_episodes(rowid, content) VALUES (new.id, new.content);
END;
CREATE TRIGGER IF NOT EXISTS episodes_ad AFTER DELETE ON episodes BEGIN
  INSERT INTO fts_episodes(fts_episodes, rowid, content) VALUES('delete', old.id, old.content);
END;

-- ─── People graph ─────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS people (
  id               INTEGER PRIMARY KEY AUTOINCREMENT,
  name             TEXT NOT NULL,
  company          TEXT,
  role             TEXT,
  relationship     TEXT,
  email            TEXT,
  phone            TEXT,
  notes            TEXT,
  last_interaction TEXT,
  created_at       TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  updated_at       TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_people_name ON people(name);

CREATE VIRTUAL TABLE IF NOT EXISTS vec_people USING vec0(
  embedding float[1024]
);

CREATE VIRTUAL TABLE IF NOT EXISTS fts_people USING fts5(
  name, company, role, relationship, notes,
  content='people', content_rowid='id', tokenize='porter unicode61'
);
CREATE TRIGGER IF NOT EXISTS people_ai AFTER INSERT ON people BEGIN
  INSERT INTO fts_people(rowid, name, company, role, relationship, notes)
  VALUES (new.id, new.name, new.company, new.role, new.relationship, new.notes);
END;
CREATE TRIGGER IF NOT EXISTS people_ad AFTER DELETE ON people BEGIN
  INSERT INTO fts_people(fts_people, rowid, name, company, role, relationship, notes)
  VALUES('delete', old.id, old.name, old.company, old.role, old.relationship, old.notes);
END;
CREATE TRIGGER IF NOT EXISTS people_au AFTER UPDATE ON people BEGIN
  INSERT INTO fts_people(fts_people, rowid, name, company, role, relationship, notes)
  VALUES('delete', old.id, old.name, old.company, old.role, old.relationship, old.notes);
  INSERT INTO fts_people(rowid, name, company, role, relationship, notes)
  VALUES (new.id, new.name, new.company, new.role, new.relationship, new.notes);
END;

-- ─── Interactions: people <-> moments ─────────────────────────
CREATE TABLE IF NOT EXISTS interactions (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  person_id  INTEGER NOT NULL REFERENCES people(id)   ON DELETE CASCADE,
  episode_id INTEGER          REFERENCES episodes(id) ON DELETE SET NULL,
  kind       TEXT,
  summary    TEXT,
  ts         TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE INDEX IF NOT EXISTS idx_interactions_person ON interactions(person_id, ts DESC);

-- ─── Documents: notes, emails, articles, ingested content ─────
CREATE TABLE IF NOT EXISTS documents (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  kind       TEXT NOT NULL,
  source_id  TEXT,
  title      TEXT,
  content    TEXT NOT NULL,
  metadata   TEXT,
  tags       TEXT,
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE INDEX IF NOT EXISTS idx_documents_kind    ON documents(kind);
CREATE INDEX IF NOT EXISTS idx_documents_source  ON documents(kind, source_id);
CREATE INDEX IF NOT EXISTS idx_documents_created ON documents(created_at DESC);

CREATE VIRTUAL TABLE IF NOT EXISTS vec_documents USING vec0(
  embedding float[1024]
);

CREATE VIRTUAL TABLE IF NOT EXISTS fts_documents USING fts5(
  title, content,
  content='documents', content_rowid='id', tokenize='porter unicode61'
);
CREATE TRIGGER IF NOT EXISTS documents_ai AFTER INSERT ON documents BEGIN
  INSERT INTO fts_documents(rowid, title, content) VALUES (new.id, new.title, new.content);
END;
CREATE TRIGGER IF NOT EXISTS documents_ad AFTER DELETE ON documents BEGIN
  INSERT INTO fts_documents(fts_documents, rowid, title, content)
  VALUES('delete', old.id, old.title, old.content);
END;

-- ─── Watchers: Gmail scan state (singleton row, id=1) ─────────
CREATE TABLE IF NOT EXISTS gmail_state (
  id              INTEGER PRIMARY KEY,
  last_message_id TEXT,
  last_history_id TEXT,
  last_scan_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

-- ─── Watchers: which calendar events have been pre-briefed ────
CREATE TABLE IF NOT EXISTS calendar_prep_log (
  event_id TEXT PRIMARY KEY,
  title    TEXT,
  sent_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

-- ─── Belief register: opinions Maahi forms about what Meet needs ──
CREATE TABLE IF NOT EXISTS beliefs (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  kind          TEXT NOT NULL,      -- nudge | concern | opportunity | observation
  content       TEXT NOT NULL,
  rationale     TEXT,
  confidence    REAL DEFAULT 0.5,
  evidence_json TEXT,
  created_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  surfaced_at   TEXT,
  resolved_at   TEXT,
  resolution    TEXT                 -- acted | dismissed | stale
);
CREATE INDEX IF NOT EXISTS idx_beliefs_unresolved ON beliefs(resolved_at, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_beliefs_kind       ON beliefs(kind);
