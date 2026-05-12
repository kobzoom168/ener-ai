import aiosqlite
from contextlib import asynccontextmanager
from pathlib import Path
from app.core.config import settings

DB_PATH = Path(settings.database_path)


@asynccontextmanager
async def get_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    async with aiosqlite.connect(
        str(DB_PATH),
        timeout=30.0,
    ) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("PRAGMA busy_timeout=10000")
        yield db


async def init_db():
    async with get_db() as db:
        await db.executescript("""
            CREATE TABLE IF NOT EXISTS notes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                content TEXT NOT NULL,
                category TEXT DEFAULT 'random',
                ai_summary TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                priority TEXT DEFAULT 'medium',
                deadline_hint TEXT,
                status TEXT DEFAULT 'open',
                tags TEXT DEFAULT '',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                done_at TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS memories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                key TEXT UNIQUE NOT NULL,
                value TEXT NOT NULL,
                tag TEXT DEFAULT 'general',
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS long_term_memories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                content TEXT NOT NULL,
                memory_type TEXT DEFAULT 'general',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS beliefs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                topic TEXT NOT NULL,
                belief TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS lessons_learned (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                mistake TEXT NOT NULL,
                reason TEXT,
                lesson TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS daily_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                log_date DATE NOT NULL,
                category TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS projects (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                deleted_at DATETIME NULL
            );

            CREATE TABLE IF NOT EXISTS uploads (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                filename TEXT NOT NULL,
                filepath TEXT NOT NULL,
                size_bytes INTEGER,
                summary TEXT,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS standup_projects (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                status TEXT DEFAULT 'In Progress',
                percent_complete INTEGER DEFAULT 0,
                current_status TEXT DEFAULT '',
                next_steps TEXT DEFAULT '',
                due_date TEXT DEFAULT '',
                today_tasks TEXT DEFAULT '',
                sort_order INTEGER DEFAULT 0,
                is_active INTEGER DEFAULT 1,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS news_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                url TEXT,
                source TEXT,
                summary TEXT,
                relevance TEXT,
                fetched_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS approved_news_sources (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                domain TEXT UNIQUE NOT NULL,
                rss_url TEXT NOT NULL,
                category TEXT DEFAULT 'general',
                reason TEXT,
                approved_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                active BOOLEAN DEFAULT 1
            );

            CREATE TABLE IF NOT EXISTS digests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                digest_type TEXT NOT NULL,
                period_start DATE NOT NULL,
                period_end DATE NOT NULL,
                content TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS audit_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                action TEXT NOT NULL,
                details TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS ai_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                agent TEXT NOT NULL,
                model TEXT NOT NULL,
                prompt_tokens INTEGER DEFAULT 0,
                completion_tokens INTEGER DEFAULT 0,
                response_time_ms INTEGER DEFAULT 0,
                estimated_cost_thb REAL DEFAULT 0,
                success BOOLEAN DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS server_metrics (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                cpu_percent REAL,
                ram_percent REAL,
                ram_used_mb INTEGER,
                ram_total_mb INTEGER,
                disk_percent REAL,
                net_in_bytes INTEGER DEFAULT 0,
                net_out_bytes INTEGER DEFAULT 0,
                recorded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS agent_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                agent_name TEXT NOT NULL,
                triggered_by TEXT DEFAULT 'user',
                input_summary TEXT,
                output_summary TEXT,
                model_used TEXT,
                duration_ms INTEGER DEFAULT 0,
                success BOOLEAN DEFAULT 1,
                error_msg TEXT,
                cost_thb REAL DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS session_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                log_date DATE NOT NULL UNIQUE,
                key_insights TEXT,
                decisions_made TEXT,
                things_failed TEXT,
                open_questions TEXT,
                next_focus TEXT,
                raw_summary TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS agent_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_type TEXT NOT NULL,
                agent_name TEXT NOT NULL,
                triggered_by TEXT DEFAULT 'user',
                tags TEXT DEFAULT '[]',
                summary TEXT NOT NULL,
                context TEXT,
                result TEXT DEFAULT 'success',
                learned TEXT,
                related_event_id INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS app_config (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL DEFAULT '',
                description TEXT DEFAULT '',
                is_secret INTEGER DEFAULT 1,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS pipeline_metrics (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                complexity TEXT,
                domain TEXT,
                model_used TEXT,
                router_ms INTEGER,
                reasoner_ms INTEGER,
                checker_ms INTEGER,
                total_ms INTEGER,
                was_fixed INTEGER DEFAULT 0,
                question_preview TEXT,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS benchmark_results (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                question_id TEXT,
                category TEXT,
                question TEXT,
                model TEXT,
                answer TEXT,
                latency_ms INTEGER,
                error TEXT,
                rating INTEGER DEFAULT 0,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS routing_config (
                intent TEXT PRIMARY KEY,
                model TEXT NOT NULL,
                label TEXT,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS code_change_requests (
                id TEXT PRIMARY KEY,
                feature_request TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'planning',
                plan_summary TEXT,
                proposed_diff TEXT,
                proposed_files_json TEXT,
                approval_token TEXT,
                base_commit TEXT,
                work_branch TEXT,
                attempt_count INTEGER DEFAULT 0,
                max_attempts INTEGER DEFAULT 3,
                last_error TEXT,
                approved_at DATETIME,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS code_change_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                request_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                message TEXT,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS platform_projects (
                id TEXT PRIMARY KEY,
                name TEXT UNIQUE NOT NULL,
                slug TEXT UNIQUE NOT NULL,
                display_name TEXT,
                type TEXT DEFAULT 'nodejs',
                status TEXT DEFAULT 'stopped',
                port INTEGER,
                domain TEXT,
                repo_path TEXT,
                compose_path TEXT,
                server_id TEXT DEFAULT 'local',
                memory_limit TEXT DEFAULT '768m',
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                last_deploy DATETIME
            );

            CREATE TABLE IF NOT EXISTS platform_servers (
                id TEXT PRIMARY KEY,
                provider TEXT DEFAULT 'hetzner',
                name TEXT NOT NULL,
                public_ip TEXT,
                status TEXT DEFAULT 'active',
                cpu_cores INTEGER,
                ram_mb INTEGER,
                disk_gb INTEGER,
                monthly_cost_eur REAL,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS platform_metrics (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id TEXT NOT NULL,
                timestamp INTEGER NOT NULL,
                cpu_percent REAL,
                memory_mb INTEGER,
                memory_limit_mb INTEGER,
                status TEXT,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS platform_deploys (
                id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL,
                status TEXT DEFAULT 'pending',
                git_commit TEXT,
                log TEXT,
                deployed_at DATETIME DEFAULT CURRENT_TIMESTAMP
            );

            CREATE INDEX IF NOT EXISTS idx_agent_events_agent
                ON agent_events(agent_name, result, created_at);
            CREATE INDEX IF NOT EXISTS idx_agent_events_tags
                ON agent_events(tags);
            CREATE UNIQUE INDEX IF NOT EXISTS idx_news_url_date
                ON news_items(url, date(datetime(fetched_at, '+7 hours')));
        """)
        cursor = await db.execute("PRAGMA table_info(memories)")
        columns = await cursor.fetchall()
        column_names = {row["name"] for row in columns}
        if "tag" not in column_names:
            await db.execute("ALTER TABLE memories ADD COLUMN tag TEXT DEFAULT 'general'")
        cursor = await db.execute("PRAGMA table_info(ai_runs)")
        ai_run_columns = {row["name"] for row in await cursor.fetchall()}
        if "response_time_ms" not in ai_run_columns:
            await db.execute("ALTER TABLE ai_runs ADD COLUMN response_time_ms INTEGER DEFAULT 0")
        cursor = await db.execute("PRAGMA table_info(messages)")
        message_columns = {row["name"] for row in await cursor.fetchall()}
        if "project_id" not in message_columns:
            await db.execute("ALTER TABLE messages ADD COLUMN project_id INTEGER REFERENCES projects(id)")
        if "source" not in message_columns:
            await db.execute("ALTER TABLE messages ADD COLUMN source TEXT DEFAULT 'telegram'")
        await db.executescript("""
            INSERT OR IGNORE INTO standup_projects
            (id, name, status, percent_complete, current_status, next_steps, due_date, today_tasks, sort_order)
            VALUES
            (1, 'Cloud Contact Center และ Cloud PBX', 'In Progress', 5,
             'PBX เอาระบบขึ้นภายใน May 2026 เปลี่ยนระบบจาก PABX เป็น PBX Phone System บน Cloud',
             '1. ทบทวน TOR และ BOQ (ฉบับปรับปรุง)\n2. สรุป Proposal และคัดเลือก Vendor Phase 1\n3. แผนรองรับการขยาย Scalability & Integration',
             'กรกฎาคม 2569',
             'หา Headphone for Call Center\nตาม Scope of Work\nสรุปเรื่อง Report ค่า AVG การรอสาย',
             1),
            (2, 'Backup Solution', 'In Progress', 85,
             'ติดตั้งเสร็จ เหลือ Backup to AWS + Training + Document',
             'Meeting config backup to AWS 14:00\nทำ Document ให้เสร็จ',
             '16-May-2026',
             '14:00 Meeting config backup to AWS',
             2),
            (3, 'จัดหา Storage', 'In Progress', 20,
             'อัปเดตราคาใหม่ เครื่องมือแพทย์ DB DICOM file network 25gb',
             'อัปเดตราคา\nเสนอ solution',
             'Jun-Jul 2026',
             'ตามใบเสนอราคา',
             3),
            (4, 'Host VM Resource', 'In Progress', 30,
             'นัดสรุป solution',
             'ติดตามราคา\nนัด final solution',
             'May 2026',
             'ตามใบเสนอราคา + นัด final solution',
             4),
            (5, 'Improvement New Network', 'In Progress', 5,
             'อยู่ในขั้นวางแผน',
             'วางแผน infrastructure ใหม่',
             'Dec 2026',
             '',
             5);
        """)
        await db.executemany(
            """
            INSERT OR IGNORE INTO app_config (key, value, description, is_secret)
            VALUES (?, ?, ?, ?)
            """,
            [
                ("line_channel_access_token", "", "LINE Messaging API Channel Access Token", 1),
                ("line_to", "", "LINE User/Group ID to send messages (U... or C...)", 0),
                ("active_model", "auto", "AI model: auto, haiku, groq, gemini, qwen3b, qwen7b", 0),
                ("standup_auto_send_line", "false", "Auto-send standup to LINE at 07:30", 0),
                ("standup_mention", "@Noom", "LINE mention tag in standup report", 0),
                ("telegram_chat_id", str(settings.telegram_chat_id or "").strip(), "Owner Telegram Chat ID", 0),
                ("xai_api_key", "", "Grok xAI API Key (console.x.ai)", 1),
                ("deepseek_api_key", "", "DeepSeek API Key (platform.deepseek.com)", 1),
                ("moonshot_api_key", "", "Kimi Moonshot API Key (platform.moonshot.cn)", 1),
                ("openai_api_key", "", "OpenAI API Key (platform.openai.com)", 1),
            ],
        )
        await db.executemany(
            """
            INSERT OR IGNORE INTO routing_config (intent, model, label)
            VALUES (?, ?, ?)
            """,
            [
                ("default_chat",    "groq",            "Chat ทั่วไป"),
                ("task_note",       "groq",            "Task / Note / Memory"),
                ("location",        "gemini",          "หาสถานที่ / แผนที่"),
                ("news",            "gemini",          "ข่าว / ข้อมูลปัจจุบัน"),
                ("tarot",           "haiku",           "ดวง / ทาโรต์"),
                ("ener_scan",       "haiku",           "Ener Scan / Content / Caption"),
                ("code",            "groq",            "Code / GitHub / Debug"),
                ("vendor_analysis", "deepseek-direct", "วิเคราะห์ Vendor / Hospital IT"),
                ("email_draft",     "haiku",           "Email / Draft / รายงาน"),
                ("brainstorm",      "deepseek-direct", "Brainstorm / แผน / Strategy"),
                ("critical",        "sonnet",          "ตัดสินใจสำคัญ"),
                ("system",          "groq",            "ระบบ / Code introspection"),
                ("code_agent",      "haiku",           "Code Agent / แก้ไฟล์จริง"),
            ],
        )
        await db.execute(
            """INSERT OR IGNORE INTO platform_servers
               (id, provider, name, public_ip, status, cpu_cores, ram_mb, disk_gb, monthly_cost_eur)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            ("local", "hetzner", "CPX32-main", "204.168.246.103",
             "active", 4, 8192, 80, 16.49),
        )
        await db.commit()


async def get_system_stats() -> dict:
    async with get_db() as db:
        stats = {}
        for table, label in [
            ("messages", "messages"),
            ("notes", "notes"),
            ("tasks", "tasks"),
            ("memories", "memories"),
            ("long_term_memories", "long_term_memories"),
            ("ai_runs", "ai_runs"),
            ("uploads", "uploads"),
            ("standup_projects", "standup_projects"),
        ]:
            try:
                cur = await db.execute(f"SELECT COUNT(*) AS c FROM {table}")
                row = await cur.fetchone()
                stats[label] = row["c"] if row else 0
            except Exception:
                stats[label] = 0
        try:
            cur = await db.execute(
                "SELECT COUNT(*) AS c FROM tasks WHERE status = 'open'"
            )
            row = await cur.fetchone()
            stats["open_tasks"] = row["c"] if row else 0
        except Exception:
            stats["open_tasks"] = 0
    return stats


async def get_config(key: str, default: str = "") -> str:
    async with get_db() as db:
        cur = await db.execute(
            "SELECT value FROM app_config WHERE key = ?",
            (key,),
        )
        row = await cur.fetchone()

        if key == "active_model":
            memory_cur = await db.execute(
                "SELECT value FROM memories WHERE key = ? LIMIT 1",
                ("active_model",),
            )
            memory_row = await memory_cur.fetchone()
            if memory_row and memory_row["value"]:
                return str(memory_row["value"])

    return str(row["value"]) if row and row["value"] else default


async def set_config(key: str, value: str) -> None:
    normalized_value = str(value or "")
    async with get_db() as db:
        await db.execute(
            """
            INSERT INTO app_config (key, value, updated_at)
            VALUES (?, ?, datetime('now'))
            ON CONFLICT(key) DO UPDATE SET
                value = excluded.value,
                updated_at = excluded.updated_at
            """,
            (key, normalized_value),
        )
        if key == "active_model":
            await db.execute(
                """
                INSERT INTO memories (key, value, tag)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value = excluded.value,
                    tag = excluded.tag,
                    updated_at = CURRENT_TIMESTAMP
                """,
                ("active_model", normalized_value, "system"),
            )
        await db.commit()


async def get_all_config() -> list[dict]:
    async with get_db() as db:
        cur = await db.execute(
            """
            SELECT key, value, description, is_secret, updated_at
            FROM app_config
            ORDER BY key
            """
        )
        rows = await cur.fetchall()
    return [dict(row) for row in rows]


async def get_pending_code_request(token: str) -> dict | None:
    async with get_db() as db:
        cur = await db.execute(
            "SELECT * FROM code_change_requests WHERE approval_token=? AND status='pending_approval'",
            (token,),
        )
        row = await cur.fetchone()
    return dict(row) if row else None


async def update_code_request_status(request_id: str, status: str, **kwargs) -> None:
    fields = ["status=?", "updated_at=datetime('now')"]
    values: list = [status]
    for k, v in kwargs.items():
        fields.append(f"{k}=?")
        values.append(v)
    values.append(request_id)
    async with get_db() as db:
        await db.execute(
            f"UPDATE code_change_requests SET {', '.join(fields)} WHERE id=?",
            values,
        )
        await db.commit()
