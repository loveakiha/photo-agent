-- M3c-v2: Photo Semantic Profile (user design doc 2026-09-22).
-- The old flat columns (scene/subjects/person/context/semantic_score) stay
-- as DERIVED views of the rich profile so all existing consumers
-- (intent/pipeline/taste/reports) keep working on v2 rows unchanged.
-- profile      : the full structured observation (JSON).
-- rel_text     : flattened relationships ("subject verb object; ...") as
--                a plain-text column so SQL LIKE can do coarse recall for
--                M3.4 without parsing JSON row by row.

ALTER TABLE semantic_analysis ADD COLUMN profile TEXT;
ALTER TABLE semantic_analysis ADD COLUMN rel_text TEXT;

INSERT OR REPLACE INTO meta (key, value) VALUES ('schema_version', '6');
