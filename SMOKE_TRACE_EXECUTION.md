# Ener-AI Core V2 Trace Execution Smoke

## 1) Compile check

```bash
python -m compileall app
```

## 2) Run AI and capture trace

```bash
curl -s -X POST http://127.0.0.1:8000/ai/run \
  -H 'Content-Type: application/json' \
  -d '{"source":"api","chat_id":"smoke-trace","text":"hello from smoke trace"}'
```

Expected: JSON includes `trace_id` and `conversation_id`.

## 3) Trigger tool run

Use a prompt likely to call memory/task tools:

```bash
curl -s -X POST http://127.0.0.1:8000/ai/run \
  -H 'Content-Type: application/json' \
  -d '{"source":"api","chat_id":"smoke-trace","text":"ช่วยบันทึกโน้ตว่า ทดสอบ tool_runs"}'
```

Then verify:

```sql
SELECT trace_id, tool_name, success, duration_ms, created_at
FROM tool_runs
ORDER BY id DESC
LIMIT 10;
```

## 4) Trigger code run (propose path)

Ask for code change proposal via AI route (or use `propose_code_change` flow).
Expected: `code_runs` has `action='propose'` and `status='pending_approval'`.

```sql
SELECT trace_id, request_id, action, status, created_at
FROM code_runs
ORDER BY id DESC
LIMIT 10;
```

## 5) Trace API contains tool/code runs

```bash
curl -s http://127.0.0.1:8000/admin/api/ai-traces/recent?limit=5
```

Expected each item includes:
- `trace_id`, `conversation_id`, `user_preview`, `assistant_preview`
- `tool_runs` (list)
- `code_runs` (list)
