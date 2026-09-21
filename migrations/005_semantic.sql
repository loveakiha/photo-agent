-- M3c: semantic_analysis (IDEA §M3c — structured VLM observations, cached).
-- One row per (photo_id, model, prompt_version, analysis_version): swapping
-- the model or the prompt re-analyzes without losing history.
--
-- NOTE: photo_id is UNIQUE (not PK) so the FK cascade from purge works and
-- so re-analysis under the same versions overwrites in place.

CREATE TABLE IF NOT EXISTS semantic_analysis (
  photo_id            INTEGER NOT NULL,
  model               TEXT NOT NULL,
  prompt_version      TEXT NOT NULL,
  analysis_version    TEXT NOT NULL,
  scene               TEXT,
  subjects            TEXT,
  person              TEXT,
  defects             TEXT,
  context             TEXT,
  semantic_score      REAL,
  score_components    TEXT,
  raw_response        TEXT,
  model_version       TEXT,
  created_at          TEXT,
  UNIQUE (photo_id, model, prompt_version, analysis_version),
  FOREIGN KEY(photo_id) REFERENCES photos(photo_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_semantic_photo ON semantic_analysis(photo_id);
CREATE INDEX IF NOT EXISTS idx_semantic_scene ON semantic_analysis(scene);

INSERT OR REPLACE INTO meta (key, value) VALUES ('schema_version', '5');
