-- 兼容性增量迁移：需求状态不再与会话列表预览共用字段。
-- 旧 preview 中有效的需求 JSON 由读取路径兼容，下次更新写入新字段。
ALTER TABLE sessions ADD COLUMN IF NOT EXISTS requirement_json TEXT;
