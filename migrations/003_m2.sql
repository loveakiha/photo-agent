-- photo-agent schema v3
-- M2: quality computation needs per-photo invalidation by content.
-- photo_id alone is not enough: after a library rebuild (photo.db reset)
-- autoincrement ids restart, so an id-keyed cache could collide with a
-- stale row. The content hash is the stable identity (same convention as
-- M0 thumbnails and M1 hash binding): a changed file forces recomputation.

ALTER TABLE quality
  ADD COLUMN sha256 TEXT;

INSERT OR REPLACE INTO meta (key, value) VALUES ('schema_version', '3');
