# Application-storage retention policy

This **manual** CLI governs only this application's PostgreSQL records. It does **not** establish that data held by payment providers, messaging systems, model providers, backups, replicas, or other third parties has been deleted. No background retention schedule is installed. An operator must review a preview, confirm eligibility and backup/legal obligations, then explicitly run `--apply`; there is no automatic deletion before verification.

From `flora_agent_package/` with the same PostgreSQL `DATABASE_URL` as the application:

```sh
python -m scripts.retention_cleanup                  # default: dry-run, 90 days
python -m scripts.retention_cleanup --days 90         # preview chosen positive duration
python -m scripts.retention_cleanup --days 90 --apply # explicit manual deletion
python -m scripts.retention_cleanup --user-id USER_ID # preview one user's owned records
```

The threshold is a UTC instant (`now - days`); only timestamps **strictly earlier** than it qualify. The JSON audit output records mode, days, UTC cutoff, owner scope, exact per-table row counts and total. Dry-run counts are estimates of currently matching records, not deletions; `--apply` counts are actual affected rows. Preserve output externally if an audit trail is required: the script creates no audit table or scheduled job. Concurrent changes between separate preview/apply commands can change counts. Apply acquires write-conflicting table locks for its single transaction, avoiding eligibility races while deleting children and parents; schedule it in a maintenance window. Any SQL error rolls back the whole transaction.

| Table | Eligible after 90 days (or `--days`) | Protected / linkage |
| --- | --- | --- |
| `merchant_tickets` | `status = 'closed'` and `updated_at` expired | `open` and `forwarded` stay, even when old. |
| `reminders` | `status IN ('cancelled', 'done')` and `updated_at` expired | Runtime uses **`done`** for a completed one-shot reminder (not `completed`). `active` and `paused` remain, regardless of age. |
| `notifications` | `is_read = 1` and `created_at` expired | Unread (`is_read = 0`) never qualifies. The schema does not store read time; age is based on creation, so a newly read old item qualifies. |
| `sessions` | `updated_at` expired | No active status exists; `updated_at` is the application's inactivity proxy. A session updated recently is retained. |
| `messages` | All messages linked by `session_id` to an expired session | Messages of retained sessions remain, even if individually old. Unlinked/orphan messages are not included. Children are removed before sessions in the same transaction. |
| `call_logs` | `created_at` expired | Independently aged logging records. |
| `tool_call_logs` | `created_at` expired, **or** parent `call_logs.created_at` expired | Child logs of an expired parent are deleted first, including newer children; logs on a retained parent expire by their own timestamp. |
| `speech_logs`, `customer_service_logs` | `created_at` expired | Log-only records, not business state. |

`--user-id` uses exact parameterized equality on the `user_id` fields; children must belong to an eligible parent owned by that user. `customer_service_logs` has no user identifier and is **excluded** from user-scoped runs (reported as zero); omit `--user-id` for the global age-based policy. SQL table names and predicates are fixed constants; dates and user ID are bound values, never interpolated. Tables not listed here (for example memories, preferences, plans, image tasks, mapping audit, and external systems) are outside this policy.

To test without a database: `python -m pytest tests/test_retention_cleanup.py -q`. The test suite uses a mock connection/transaction and does not delete real data. Run a real preview and review its JSON counts before any production application.
