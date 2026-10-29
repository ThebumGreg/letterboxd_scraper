-- Recordings as their own state. A recording's length is saved so SyncHub can say whether it is a full recording; the new state is used only when the
-- setting dvr_counts_as_library is off (by default a recording counts as In Library, as before).
ALTER TABLE src_jellyfin_item ADD COLUMN runtime_seconds INTEGER;
INSERT INTO state_def (code, label, rank, color, icon, is_terminal) VALUES
    ('recorded', 'Recorded (DVR)', 90, 'info', 'bi-record-circle', 0);
