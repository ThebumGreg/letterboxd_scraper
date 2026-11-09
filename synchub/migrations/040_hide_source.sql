-- Hiding the original of a copied film from Jellyfin: a worker puts an empty .ignore file in the original's folder.
ALTER TABLE library_file ADD COLUMN source_hidden_at TEXT;
ALTER TABLE library_file ADD COLUMN source_hide_message TEXT;
