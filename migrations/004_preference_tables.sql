-- photo-agent schema v4
-- Preference / Creative Search reservation (IDEA.md §22, M3.0 era).
--
-- These tables are STRUCTURE-ONLY at this stage: no code path writes them
-- yet, M3.1+ (Taste Profile, Intent) and the future Creative Search engine
-- will populate them. They exist now so the data model is stable before
-- first real preference signals arrive.
--
-- preference_samples: one row = one pairwise (or N-way) preference signal.
--   candidate_a / candidate_b / winner are photo_ids; winner may be NULL
--   for un-decided samples. `source` distinguishes the signal's origin:
--     user          - explicit user choice (M3.1+)
--     model         - model/user disagreement case (active learning)
--     vlm           - Qwen 27B teacher annotation (data flywheel, §7)
--     inference     - behavior-inferred (accept / delete / replace, M3.5+)
--   `confidence` is [0,1] when the source provides one, else NULL.
--
-- creative_candidates: one row = one generated (or sourced) creative work
--   evaluated by the selection pipeline. `human_selection` is set when a
--   human final-picks it; NULL until then. `rarity` / `tier` are reserved
--   for the SSR-tier concept (§8) and stay NULL until tier logic exists.

CREATE TABLE IF NOT EXISTS preference_samples (
  sample_id       INTEGER PRIMARY KEY AUTOINCREMENT,
  candidate_a     INTEGER,
  candidate_b     INTEGER,
  winner          INTEGER,
  context         TEXT,
  source          TEXT NOT NULL DEFAULT 'user',
  confidence      REAL,
  reason          TEXT,
  user_id         TEXT NOT NULL DEFAULT 'local',
  timestamp       TEXT NOT NULL,
  FOREIGN KEY(candidate_a) REFERENCES photos(photo_id),
  FOREIGN KEY(candidate_b) REFERENCES photos(photo_id),
  FOREIGN KEY(winner)       REFERENCES photos(photo_id)
);
CREATE INDEX IF NOT EXISTS idx_pref_samples_winner ON preference_samples(winner);
CREATE INDEX IF NOT EXISTS idx_pref_samples_user   ON preference_samples(user_id);
CREATE INDEX IF NOT EXISTS idx_pref_samples_source ON preference_samples(source);

CREATE TABLE IF NOT EXISTS creative_candidates (
  candidate_id       INTEGER PRIMARY KEY AUTOINCREMENT,
  prompt             TEXT,
  seed               INTEGER,
  model              TEXT,
  parameters         TEXT,           -- JSON blob (sampler/CFG/LoRA/etc.)
  aesthetic_score    REAL,
  alignment_score    REAL,
  artifact_score     REAL,
  human_selection    INTEGER,        -- NULL until a human final-picks
  rarity             TEXT,           -- reserved for SSR tier (§8); NULL now
  tier               TEXT,           -- reserved for SSR tier (§8); NULL now
  created_at         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_creative_human ON creative_candidates(human_selection);
CREATE INDEX IF NOT EXISTS idx_creative_model ON creative_candidates(model);

INSERT OR REPLACE INTO meta (key, value) VALUES ('schema_version', '4');
