-- photo-agent schema v2
-- M1: record how and when perceptual hashes were computed, so future
-- algorithm upgrades (m1-v2, ...) can be distinguished from m1-v1 rows.

ALTER TABLE photo_hashes
  ADD COLUMN algorithm_version TEXT;

ALTER TABLE photo_hashes
  ADD COLUMN computed_at TEXT;

INSERT OR REPLACE INTO meta (key, value) VALUES ('schema_version', '2');
