-- photo-agent schema v1
-- M0: one photos row represents one scanned filesystem file instance.

CREATE TABLE IF NOT EXISTS meta (
  key   TEXT PRIMARY KEY,
  value TEXT
);

CREATE TABLE IF NOT EXISTS photos (
  photo_id     INTEGER PRIMARY KEY AUTOINCREMENT,
  rel_path     TEXT NOT NULL,
  abs_path     TEXT NOT NULL,
  sha256       TEXT NOT NULL,
  size_bytes   INTEGER,
  width        INTEGER,
  height       INTEGER,
  format       TEXT,
  taken_at     TEXT,
  gps_lat      REAL,
  gps_lng      REAL,
  camera_make  TEXT,
  camera_model TEXT,
  file_mtime   TEXT,
  first_seen   TEXT,
  last_scanned TEXT,
  UNIQUE(rel_path, sha256)
);
CREATE INDEX IF NOT EXISTS idx_photos_sha256 ON photos(sha256);
CREATE INDEX IF NOT EXISTS idx_photos_rel_path ON photos(rel_path);
CREATE INDEX IF NOT EXISTS idx_photos_taken ON photos(taken_at);

CREATE TABLE IF NOT EXISTS photo_hashes (
  photo_id INTEGER PRIMARY KEY,
  phash    TEXT,
  dhash    TEXT,
  FOREIGN KEY(photo_id) REFERENCES photos(photo_id)
);

CREATE TABLE IF NOT EXISTS embeddings (
  photo_id   INTEGER NOT NULL,
  model      TEXT NOT NULL,
  dimension  INTEGER,
  file       TEXT,
  created_at TEXT,
  PRIMARY KEY (photo_id, model),
  FOREIGN KEY(photo_id) REFERENCES photos(photo_id)
);

CREATE TABLE IF NOT EXISTS quality (
  photo_id          INTEGER PRIMARY KEY,
  sharpness_raw     REAL,
  exposure_raw      REAL,
  noise_raw         REAL,
  sharpness         REAL,
  exposure          REAL,
  noise              REAL,
  quality_score     REAL,
  algorithm_version TEXT,
  computed_at       TEXT,
  FOREIGN KEY(photo_id) REFERENCES photos(photo_id)
);

CREATE TABLE IF NOT EXISTS vlm_sheets (
  sheet_id           TEXT PRIMARY KEY,
  photo_ids          TEXT,
  scene              TEXT,
  obvious_duplicates TEXT,
  obvious_bad        TEXT,
  keep_candidates    TEXT,
  notes              TEXT,
  model              TEXT,
  prompt_version     TEXT,
  created_at         TEXT
);

CREATE TABLE IF NOT EXISTS vlm_analysis (
  photo_id           INTEGER NOT NULL,
  pass               TEXT NOT NULL,
  sheet_id           TEXT,
  sheet_flag         TEXT,
  technical_quality  TEXT,
  composition        REAL,
  expression_quality REAL,
  scene_value        REAL,
  subject            TEXT,
  scene              TEXT,
  people             TEXT,
  keep               TEXT,
  reason             TEXT,
  model              TEXT,
  prompt_version     TEXT,
  created_at         TEXT,
  PRIMARY KEY (photo_id, pass),
  FOREIGN KEY(photo_id) REFERENCES photos(photo_id)
);

CREATE TABLE IF NOT EXISTS groups (
  group_id     INTEGER PRIMARY KEY AUTOINCREMENT,
  kind         TEXT NOT NULL,
  rep_photo_id INTEGER,
  size         INTEGER,
  created_at   TEXT,
  FOREIGN KEY(rep_photo_id) REFERENCES photos(photo_id)
);
CREATE INDEX IF NOT EXISTS idx_groups_kind ON groups(kind);

CREATE TABLE IF NOT EXISTS group_members (
  group_id      INTEGER NOT NULL,
  photo_id      INTEGER NOT NULL,
  recall_source TEXT,
  sim_to_rep    REAL,
  PRIMARY KEY (group_id, photo_id),
  FOREIGN KEY(group_id) REFERENCES groups(group_id),
  FOREIGN KEY(photo_id) REFERENCES photos(photo_id)
);
CREATE INDEX IF NOT EXISTS idx_group_members_photo ON group_members(photo_id);

CREATE TABLE IF NOT EXISTS decisions (
  photo_id     INTEGER PRIMARY KEY,
  status       TEXT NOT NULL,
  final_score  REAL,
  reason       TEXT,
  source       TEXT,
  rule_version TEXT,
  updated_at   TEXT,
  FOREIGN KEY(photo_id) REFERENCES photos(photo_id)
);

CREATE TABLE IF NOT EXISTS runs (
  run_id      INTEGER PRIMARY KEY AUTOINCREMENT,
  stage       TEXT,
  started_at  TEXT,
  finished_at TEXT,
  params      TEXT
);

INSERT OR REPLACE INTO meta (key, value) VALUES ('schema_version', '1');
INSERT OR REPLACE INTO meta (key, value) VALUES ('project_version', '0.1.0');
