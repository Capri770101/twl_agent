-- Flora Agent: image task persistence
-- Execute once when the deployment database is managed by a DBA.
-- The application also creates this table automatically when it has DDL rights.

CREATE TABLE IF NOT EXISTS image_tasks (
    task_id TEXT PRIMARY KEY,
    user_id TEXT,
    status TEXT NOT NULL,
    prompt TEXT NOT NULL,
    result_url TEXT,
    error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_image_tasks_updated
    ON image_tasks(updated_at DESC);

ALTER TABLE image_tasks ADD COLUMN IF NOT EXISTS user_id TEXT;

CREATE INDEX IF NOT EXISTS idx_image_tasks_user
    ON image_tasks(user_id, updated_at DESC);
