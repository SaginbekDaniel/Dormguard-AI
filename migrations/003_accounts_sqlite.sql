-- Compatible v2 upgrade. Caller holds BEGIN IMMEDIATE on a dedicated
-- FK-disabled connection and checks all FKs before committing the replacement.
-- Copy every existing identity unchanged. No child rows are deleted.
CREATE TABLE users_v3(id TEXT PRIMARY KEY,email TEXT UNIQUE NOT NULL,role TEXT UNIQUE NOT NULL CHECK(role IN ('director','deputy_director','technical_admin','super_admin')),password_hash TEXT,enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0,1)),created_at TEXT NOT NULL,display_name TEXT NOT NULL DEFAULT '' CHECK(length(display_name)<=80));
INSERT INTO users_v3(id,email,role,password_hash,enabled,created_at) SELECT id,email,role,password_hash,enabled,created_at FROM users;
DROP TABLE users;
ALTER TABLE users_v3 RENAME TO users;
