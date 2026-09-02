"""内部 PostgreSQL 中的映射草案存储与审计。"""
from __future__ import annotations

import json
import uuid
from typing import Any

from psycopg.types.json import Jsonb

from backend.storage.db import transaction


def save_mapping_draft(profile: dict[str, Any], draft: dict[str, Any], actor: str = 'agent') -> dict[str, Any]:
    source_id = str(profile.get('source_id', '')).strip()
    schema = str(profile.get('schema', 'public')).strip()
    fingerprint = str(profile.get('schema_fingerprint', '')).strip()
    if not source_id or not fingerprint:
        raise ValueError('profile requires source_id and schema_fingerprint')
    mapping_id = str(uuid.uuid4())
    with transaction() as conn:
        row = conn.execute('SELECT COALESCE(MAX(version), 0) + 1 AS version FROM mapping_drafts WHERE source_id=? AND schema_fingerprint=?', (source_id, fingerprint)).fetchone()
        version = int(row['version'])
        conn.execute('INSERT INTO mapping_drafts (id, source_id, schema_name, schema_fingerprint, version, status, draft_json, created_by) VALUES (?, ?, ?, ?, ?, ?, ?, ?)', (mapping_id, source_id, schema, fingerprint, version, 'draft', Jsonb(draft), actor))
        conn.execute('INSERT INTO mapping_audit (source_id, mapping_id, action, actor, details) VALUES (?, ?, ?, ?, ?)', (source_id, mapping_id, 'draft_created', actor, Jsonb({'version': version, 'schema_fingerprint': fingerprint})))
    return {'id': mapping_id, 'source_id': source_id, 'schema': schema, 'schema_fingerprint': fingerprint, 'version': version, 'status': 'draft'}


def update_mapping_status(mapping_id: str, status: str, actor: str) -> dict[str, Any]:
    allowed = {'reviewed', 'approved', 'active', 'revoked'}
    if status not in allowed:
        raise ValueError(f'invalid mapping status: {status}')
    if not mapping_id or not actor:
        raise ValueError('mapping_id and actor are required')
    with transaction() as conn:
        row = conn.execute('SELECT id, source_id, status, version FROM mapping_drafts WHERE id=?', (mapping_id,)).fetchone()
        if not row:
            raise ValueError('mapping draft not found')
        if status == 'active':
            conn.execute("UPDATE mapping_drafts SET status='revoked' WHERE source_id=? AND status='active' AND id<>?", (row['source_id'], mapping_id))
        conn.execute('UPDATE mapping_drafts SET status=?, reviewed_by=?, reviewed_at=NOW() WHERE id=?', (status, actor, mapping_id))
        conn.execute('INSERT INTO mapping_audit (source_id, mapping_id, action, actor, details) VALUES (?, ?, ?, ?, ?)', (row['source_id'], mapping_id, f'mapping_{status}', actor, Jsonb({'previous_status': row['status'], 'version': row['version']})))
    return {'id': mapping_id, 'source_id': row['source_id'], 'version': row['version'], 'status': status, 'actor': actor}


def list_mapping_drafts(source_id: str, limit: int = 20) -> list[dict[str, Any]]:
    limit = max(1, min(int(limit or 1), 100))
    with transaction() as conn:
        rows = conn.execute('SELECT id, source_id, schema_name, schema_fingerprint, version, status, created_by, created_at, reviewed_by, reviewed_at FROM mapping_drafts WHERE source_id=? ORDER BY created_at DESC LIMIT ?', (source_id, limit)).fetchall()
    return [dict(row) for row in rows]


def get_active_mapping(source_id: str) -> dict[str, Any] | None:
    if not source_id:
        raise ValueError('source_id is required')
    with transaction() as conn:
        row = conn.execute("SELECT id, source_id, schema_name, schema_fingerprint, version, status, draft_json FROM mapping_drafts WHERE source_id=? AND status='active' ORDER BY version DESC LIMIT 1", (source_id,)).fetchone()
    return dict(row) if row else None
