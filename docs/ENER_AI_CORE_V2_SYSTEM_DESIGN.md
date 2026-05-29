# Ener-AI Core v2 — System Design

**Version:** 0.1  
**Owner:** Kob / Ener-AI  
**Goal:** เปลี่ยน Ener-AI จากระบบ AI หลาย agent ให้เป็น **Local-first AI Gateway + Project-aware Memory Brain** บน server ของตัวเอง

---

## 1. Product Vision

Ener-AI คือ AI Operating System ส่วนตัวบน server ของกบเอง

หลักคิด:

1. **Server is the source of truth**  
   ข้อมูลจริงทั้งหมดต้องอยู่บน server ของกบ ไม่ผูกกับ model ใด model หนึ่ง

2. **Models are replaceable engines**  
   Claude, GPT, Gemini, Groq, Grok, DeepSeek, Kimi, Qwen local เป็นแค่ engine ที่สลับได้

3. **Local context first, external second**  
   ทุกคำถามต้องค้นข้อมูลบน server ก่อน ถ้า context ไม่พอค่อยเรียก external model / web / API

4. **Every interaction must be saved**  
   ทุกคำถาม คำตอบ tool call model call code run ต้อง trace กลับได้

5. **Project-aware by default**  
   ทุกข้อมูลต้องผูกกับ project เช่น `ener-ai-core`, `ener-scan`, `hospital-work`, `amulet-business`

6. **AI can act, but important actions need approval**  
   AI เขียน code / แก้ไฟล์ / deploy ได้ แต่ action สำคัญต้องมี approval, diff, log, rollback

---

## 2. Current System Summary

### Existing stack

- FastAPI + Python
- SQLite
- Telegram bot
- Web Admin
- Docker on Hetzner VPS
- GitHub CI
- Ollama / Qwen local
- External models:
  - Claude Haiku / Sonnet / Opus
  - GPT-4o / GPT-4o mini
  - Gemini
  - Groq
  - DeepSeek
  - xAI Grok
  - Kimi
- Separate app:
  - Ener Scan: Node.js / Express + LINE bot + report page

### Existing strengths

- `app/core/ai.py`
  - multi-model core
  - model fallback
  - `ai_runs` logging

- `app/core/reasoning_pipeline.py`
  - intent routing
  - pipeline metrics
  - tool selection

- `app/core/context_builder.py`
  - local context builder already exists
  - currently too narrow

- `app/core/code_agent.py`
  - autonomous code change request core
  - approval token
  - safe paths
  - patch apply
  - syntax check
  - git commit / push / deploy

- `app/core/database.py`
  - many useful tables already exist
  - messages, memories, long_term_memories, ai_runs, agent_events, code_change_requests, projects, uploads, tasks

---

## 3. Main Problems To Solve

1. ไม่มี AI Gateway กลางที่ทุก channel ต้องผ่าน
2. บาง agent ยังเรียก model / pipeline เอง
3. local context ยังไม่ถูกบังคับใช้ทุก request
4. `messages` metadata ยังไม่พอ
5. `ai_runs` ยังไม่ผูกกับ message / conversation / trace
6. `/code` ยังเป็น code generator มากกว่า code workflow
7. admin UI มีหลายหน้าเกินไป
8. project ยังไม่เป็นหน่วยหลักของข้อมูลทั้งหมด

---

## 4. Target Architecture

```text
Telegram / Web Chat / Admin / Code Agent / Ener Scan / Gmail / GitHub
        |
        v
+----------------------+
|   AI Gateway         |
|   app/core/ai_gateway.py
+----------------------+
        |
        | 1. normalize request
        | 2. detect project
        | 3. detect intent
        | 4. save user message
        | 5. build local context
        | 6. route model
        | 7. call model/tool
        | 8. save assistant message
        | 9. save trace/log/run
        v
+----------------------+
| Local Data Layer     |
| SQLite + FTS5        |
+----------------------+
        |
        v
+----------------------+
| Model Engines        |
| Claude/GPT/Gemini/...|
| Qwen local           |
+----------------------+
```

---

## 5. Core Components

### 5.1 AI Gateway

New file:

```text
app/core/ai_gateway.py
```

Primary function:

```python
async def run_ai(
    message: str,
    chat_id: str | None = None,
    source: str = "telegram",
    project_id: int | None = None,
    conversation_id: str | None = None,
    intent: str | None = None,
    preferred_model: str | None = None,
    allow_external_model: bool = True,
    allow_external_search: bool = False,
    metadata: dict | None = None,
) -> dict:
    ...
```

Return shape:

```python
{
    "reply": "...",
    "trace_id": "...",
    "conversation_id": "...",
    "project_id": 1,
    "intent": "code",
    "model_used": "groq",
    "external_used": false,
    "context_summary": "...",
    "context_sources": [...]
}
```

Responsibilities:

1. Create `trace_id`
2. Resolve project
3. Resolve conversation
4. Save user message
5. Route intent/model
6. Build local context
7. Call model through `ai.py`
8. Save assistant message
9. Save `ai_runs`, `tool_runs`, `context_snapshot`
10. Return response

---

### 5.2 Context Builder v2

Existing file to refactor:

```text
app/core/context_builder.py
```

Current issue:

```python
if complexity == "simple":
    return ""
```

New rule:

> Even simple chat must load at least recent messages + project summary + relevant memory.

Pseudo-code:

```python
async def build_context_v2(
    message: str,
    project_id: int | None,
    intent: str,
    route: dict,
    conversation_id: str | None = None,
    budget_chars: int = 12000,
) -> dict:
    sources = []
    parts = []

    project = await get_project_summary(project_id)
    if project:
        parts.append(section("Project Summary", project))
        sources.append({"type": "project", "id": project_id})

    recent = await get_recent_messages(project_id, conversation_id, limit=12)
    if recent:
        parts.append(section("Recent Conversation", recent))
        sources.append({"type": "messages_recent"})

    relevant = await search_local_fts(message, project_id=project_id, limit=10)
    if relevant:
        parts.append(section("Relevant Local Memory", relevant))
        sources.append({"type": "fts"})

    tasks = await get_open_tasks(project_id, limit=8)
    if tasks:
        parts.append(section("Open Tasks", tasks))
        sources.append({"type": "tasks"})

    if intent in {"code", "debug", "code_agent"}:
        code_runs = await get_recent_code_runs(project_id, limit=5)
        parts.append(section("Recent Code Runs", code_runs))
        sources.append({"type": "code_runs"})

    if intent in {"hospital", "vendor_analysis"}:
        hospital = await get_hospital_context()
        parts.append(section("Hospital Context", hospital))
        sources.append({"type": "hospital"})

    final_context = compress_to_budget(parts, budget_chars)

    return {
        "text": final_context,
        "sources": sources,
        "summary": summarize_sources(sources),
        "needs_external": should_use_external(message, final_context, intent),
    }
```

---

## 6. Database Design

### 6.1 Add columns to `messages`

Use additive migration with `PRAGMA table_info`, not Alembic yet.

```sql
ALTER TABLE messages ADD COLUMN conversation_id TEXT;
ALTER TABLE messages ADD COLUMN intent TEXT;
ALTER TABLE messages ADD COLUMN model_used TEXT;
ALTER TABLE messages ADD COLUMN route_json TEXT;
ALTER TABLE messages ADD COLUMN context_snapshot TEXT;
ALTER TABLE messages ADD COLUMN external_used INTEGER DEFAULT 0;
ALTER TABLE messages ADD COLUMN trace_id TEXT;
```

Note:

- `context_snapshot` should be TEXT storing JSON string for SQLite compatibility
- `external_used` should be INTEGER 0/1
- `project_id` and `source` already exist in current migration, but keep guard check

Indexes:

```sql
CREATE INDEX IF NOT EXISTS idx_messages_project_created
ON messages(project_id, created_at);

CREATE INDEX IF NOT EXISTS idx_messages_conversation_created
ON messages(conversation_id, created_at);

CREATE INDEX IF NOT EXISTS idx_messages_trace
ON messages(trace_id);

CREATE INDEX IF NOT EXISTS idx_messages_intent_model
ON messages(intent, model_used);
```

---

### 6.2 Create `conversations`

```sql
CREATE TABLE IF NOT EXISTS conversations (
    id TEXT PRIMARY KEY,
    project_id INTEGER,
    source TEXT DEFAULT 'telegram',
    chat_id TEXT,
    title TEXT,
    summary TEXT,
    status TEXT DEFAULT 'active',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

---

### 6.3 Create `tool_runs`

```sql
CREATE TABLE IF NOT EXISTS tool_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trace_id TEXT,
    message_id INTEGER,
    project_id INTEGER,
    conversation_id TEXT,
    tool_name TEXT NOT NULL,
    input_json TEXT,
    output_json TEXT,
    status TEXT DEFAULT 'success',
    error TEXT,
    duration_ms INTEGER DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

---

### 6.4 Create `code_runs`

Use this to complement existing `code_change_requests`.

```sql
CREATE TABLE IF NOT EXISTS code_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trace_id TEXT,
    request_id TEXT,
    project_id INTEGER,
    conversation_id TEXT,
    goal TEXT NOT NULL,
    repo_path TEXT,
    branch TEXT,
    model_used TEXT,
    context_snapshot TEXT,
    files_read_json TEXT,
    files_changed_json TEXT,
    diff_summary TEXT,
    test_command TEXT,
    test_result TEXT,
    deploy_result TEXT,
    status TEXT DEFAULT 'planning',
    lesson_learned TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

---

### 6.5 Create FTS5 index

Start with one unified local search index.

```sql
CREATE VIRTUAL TABLE IF NOT EXISTS local_knowledge_fts USING fts5(
    source_table,
    source_id,
    project_id UNINDEXED,
    title,
    content,
    tags,
    created_at UNINDEXED
);
```

Recommended indexed sources:

- messages
- memories
- long_term_memories
- uploads
- agent_events
- code_change_requests
- code_runs
- hospital_projects
- hospital_issues
- project summaries

---

## 7. Model Routing Policy

Important distinction:

> Local data first does not mean local model first.

Correct policy:

```text
1. Search local context first
2. Select model by intent / cost / quality / availability
3. Use external search/tool only if local context is insufficient
```

Example routing:

| Intent | Default Model | External Search |
|---|---|---|
| simple_chat | groq / qwen local | no |
| memory_search | groq | no |
| code_question | groq / haiku | no |
| code_agent | haiku / sonnet | no |
| critical_decision | sonnet | optional |
| ener_scan | haiku / gpt-4o | no |
| vision | gpt-4o / gemini | no |
| news/current | gemini | yes |
| hospital_vendor | deepseek / sonnet | optional |
| private_sensitive | qwen local only | no |

---

## 8. Code Agent Workflow

### 8.1 Split `/code` into two modes

Mode A: Ask / explain / review

```text
/code explain <question>
/code ask <question>
```

Mode B: Real file change

```text
/code change <goal>
/code แก้ไฟล์จริง: <goal>
```

### 8.2 Real code change flow

```text
User request
  |
  v
AI Gateway creates trace
  |
  v
Create code_run
  |
  v
Read relevant files
  |
  v
Propose patch
  |
  v
Create code_change_request
  |
  v
Return approval token
  |
  v
User approves
  |
  v
Apply patch
  |
  v
Run syntax check / tests
  |
  v
Commit / push / deploy
  |
  v
Log result + lesson learned
```

### 8.3 Safety rules

Allowed:

- app/
- tests/
- Dockerfile
- docker-compose.yml
- requirements.txt

Denied:

- .env
- .git
- data
- backups
- secrets
- private keys

Production deploy:

- default: approval required
- safe auto-heal can be whitelist only

---

## 9. API Endpoints

Keep endpoints minimal.

### 9.1 Main AI Run

```http
POST /ai/run
```

Request:

```json
{
  "message": "แก้ terminal websocket error ให้หน่อย",
  "source": "web",
  "project_id": 1,
  "conversation_id": "conv_abc",
  "intent": "code",
  "preferred_model": "haiku",
  "allow_external_model": true,
  "allow_external_search": false
}
```

Response:

```json
{
  "ok": true,
  "reply": "...",
  "trace_id": "tr_...",
  "conversation_id": "conv_abc",
  "project_id": 1,
  "intent": "code",
  "model_used": "haiku",
  "external_used": false,
  "context_sources": []
}
```

---

### 9.2 Event Ingest

```http
POST /ai/event
```

For Ener Scan, GitHub, Gmail, system monitor events.

Request:

```json
{
  "source": "ener_scan",
  "project_id": 2,
  "event_type": "scan_report_created",
  "title": "Ener Scan Report",
  "payload": {}
}
```

---

### 9.3 Context Preview

```http
GET /ai/context-preview?project_id=1&q=terminal%20websocket
```

Use for debugging what context AI would see before calling model.

---

### 9.4 Recent Traces

```http
GET /admin/api/ai-traces/recent
```

Return last AI requests with:

- time
- project
- intent
- model
- context sources
- external used
- latency
- success/failure

---

## 10. Admin UI Restructure

Current admin pages should be grouped into 5 areas:

```text
Home
Projects
AI
Ops
Settings
```

### 10.1 Home

Show only actionable overview:

- current system health
- pending approvals
- today tasks
- latest AI traces
- model/API issues
- project alerts

### 10.2 Projects

Each project has tabs:

```text
Overview
Chat
Memory
Tasks
Files
Code Runs
Artifacts
Logs
Settings
```

Recommended initial projects:

- Ener-AI Core
- Ener Scan
- Ener Platform
- Hospital Work
- Amulet Business OS
- Personal Life

### 10.3 AI

- model status
- routing editor
- prompt profiles
- cost
- latency
- trace viewer
- context preview

### 10.4 Ops

- containers
- logs
- terminal
- deploy
- API status
- backups

### 10.5 Settings

- security
- API keys
- Telegram IDs
- LINE webhooks
- feature toggles
- backup policy

---

## 11. Ener Scan Integration

Do not rewrite Ener Scan Node.js app.

Use internal event API.

### 11.1 Ener Scan sends event to Ener-AI

```http
POST http://ener-ai:8000/ai/event
```

Example:

```json
{
  "source": "ener_scan",
  "project_slug": "ener-scan",
  "event_type": "report_created",
  "title": "New amulet scan report",
  "payload": {
    "line_user_id": "...",
    "report_id": "...",
    "report_url": "...",
    "object_type": "amulet",
    "summary": "...",
    "score": 82
  }
}
```

### 11.2 Ener-AI stores as

- artifact
- memory
- project event
- optional content seed

Future extension:

```text
Ener Scan Report
  -> Amulet Item
  -> Content Pack
  -> Lead / Customer
  -> Sale
  -> Profit Summary
```

---

## 12. Migration Strategy

Do not rewrite.

### Step 1: Add schema only

- Add columns to messages
- Add conversations
- Add tool_runs
- Add code_runs
- Add FTS5 table

No logic change yet.

### Step 2: Update context builder

- always fetch local context
- keep old function name if possible
- add `build_context_v2`

### Step 3: Add gateway beside old system

- create `ai_gateway.py`
- do not switch all agents yet

### Step 4: Move chat first

- `app/agents/chat.py` calls gateway
- keep same `run_chat(chat_id, text)` interface

### Step 5: Move code agent

- split ask/change
- connect change mode to `code_change_requests`

### Step 6: Move other agents gradually

- content
- ener
- hospital
- gmail
- github
- news

---

## 13. Implementation Priority

### Priority 1 — Foundation

Files:

```text
app/core/database.py
app/core/context_builder.py
app/agents/chat.py
```

Tasks:

- schema migration
- local context every request
- save metadata in messages

### Priority 2 — Gateway

Files:

```text
app/core/ai_gateway.py
app/core/reasoning_pipeline.py
app/core/ai.py
```

Tasks:

- add gateway
- return richer metadata from pipeline
- keep `ai.py` as model engine layer

### Priority 3 — Code Agent

Files:

```text
app/agents/code_agent.py
app/core/code_agent.py
```

Tasks:

- ask/change split
- create `code_runs`
- store lesson learned
- show approval token

### Priority 4 — Admin UI

Files:

```text
app/main.py
templates/static if any
```

Tasks:

- trace viewer
- context preview
- project workspace skeleton

### Priority 5 — Ener Scan

Files:

```text
ener-scan/src/app.js
ener-scan/src/services/*
ener-ai/app/main.py
```

Tasks:

- add internal event sender
- create `/ai/event`
- store scan report as artifact/memory

---

## 14. Quick Wins: 1-2 Days

### Day 1

1. Add message metadata columns
2. Add `trace_id`
3. Add indexes
4. Modify `_save_messages()` to save:
   - model_used
   - intent
   - route_json
   - context_snapshot
   - external_used
   - trace_id

### Day 2

1. Refactor `context_builder.py`
2. Remove simple chat empty context
3. Add local search from:
   - recent messages
   - memories
   - long_term_memories
4. Add `/ai/context-preview`

Expected result:

> Ener-AI starts remembering and tracing every conversation better without changing UI.

---

## 15. Two-Week Roadmap

### Week 1 — Local-first Foundation

| Day | Work |
|---|---|
| 1 | database migration + trace_id |
| 2 | context_builder v2 |
| 3 | save route/context/model metadata |
| 4 | create minimal ai_gateway.py |
| 5 | migrate chat.py to gateway |
| 6 | add admin trace viewer |
| 7 | test Telegram/Web/Admin compatibility |

### Week 2 — Workflow + Integration

| Day | Work |
|---|---|
| 8 | code agent ask/change split |
| 9 | connect code_change_requests to code_runs |
| 10 | add tool_runs |
| 11 | add /ai/event |
| 12 | Ener Scan event integration |
| 13 | project workspace skeleton |
| 14 | memory curator updates project summaries + lessons |

---

## 16. Definition of Done

### Gateway Done

- Every chat request has trace_id
- Every message records model_used and intent
- Context snapshot is saved
- Local context loaded before model call
- External use is marked

### Context Done

- simple chat has recent/project/memory context
- code intent has recent code context
- hospital intent has hospital project context
- context budget prevents huge prompts

### Code Agent Done

- ask mode does not modify files
- change mode creates approval token
- apply mode logs diff/test/deploy
- rollback exists on failure
- lesson learned saved

### Admin Done

- can see recent AI traces
- can inspect context preview
- can see per-project messages/memory
- old pages still accessible

---

## 17. Design Principles To Keep In Repo

```text
1. Server is the source of truth.
2. Models are replaceable engines.
3. Local context first, external second.
4. Every interaction must be saved.
5. Every message belongs to a project/conversation when possible.
6. AI actions must be traceable.
7. Code changes require approval unless explicitly whitelisted.
8. Do not rewrite what already works.
9. Prefer additive migration.
10. Keep admin simple: Home, Projects, AI, Ops, Settings.
```

---

## 18. Suggested File Path

Put this file in the repo as:

```text
docs/ENER_AI_CORE_V2_SYSTEM_DESIGN.md
```

Optional short version:

```text
systemdesign_v2.md
```
