-- PINs and permissions. A profile with no PIN can still be chosen by anyone (as before); permissions NULL means "everything".
ALTER TABLE profile ADD COLUMN pin_hash TEXT;
ALTER TABLE profile ADD COLUMN pin_version INTEGER NOT NULL DEFAULT 0;      -- raised by every PIN change and "sign out everywhere": older remembered sign-ins stop working
ALTER TABLE profile ADD COLUMN pin_failures INTEGER NOT NULL DEFAULT 0;
ALTER TABLE profile ADD COLUMN pin_locked_until TEXT;
ALTER TABLE profile ADD COLUMN permissions TEXT;

-- The key that signs cookies, kept so that restarting does not sign everyone out and every server process agrees.
CREATE TABLE app_secret (
    name TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
