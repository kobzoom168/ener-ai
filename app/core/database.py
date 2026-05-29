import logging
import aiosqlite
from contextlib import asynccontextmanager
from pathlib import Path
from app.core.config import settings

DB_PATH = Path(settings.database_path)
logger = logging.getLogger(__name__)


async def _column_names(db: aiosqlite.Connection, table: str) -> set[str]:
    cur = await db.execute(f'PRAGMA table_info("{table}")')
    return {str(r["name"]) for r in await cur.fetchall()}


async def _add_column_if_missing(
    db: aiosqlite.Connection, table: str, column: str, sql_type_default: str
) -> None:
    cols = await _column_names(db, table)
    if column in cols:
        return
    sql = f'ALTER TABLE "{table}" ADD COLUMN "{column}" {sql_type_default}'
    try:
        await db.execute(sql)
    except Exception as exc:
        logger.warning(
            "additive migration ALTER failed (table=%s column=%s): %s",
            table,
            column,
            exc,
            exc_info=True,
        )


async def _hospital_column_names(db: aiosqlite.Connection, table: str) -> set[str]:
    cur = await db.execute(f'PRAGMA table_info("{table}")')
    return {str(r["name"]) for r in await cur.fetchall()}


async def _hospital_add_column_if_missing(
    db: aiosqlite.Connection, table: str, column: str, sql_type_default: str
) -> None:
    cols = await _hospital_column_names(db, table)
    if column in cols:
        return
    sql = f'ALTER TABLE "{table}" ADD COLUMN "{column}" {sql_type_default}'
    try:
        await db.execute(sql)
    except Exception as exc:
        logger.warning(
            "hospital migration ALTER failed (table=%s column=%s): %s",
            table,
            column,
            exc,
            exc_info=True,
        )


async def _hospital_create_index_if_columns_exist(
    db: aiosqlite.Connection,
    index_name: str,
    table: str,
    columns: tuple[str, ...],
    sql: str,
) -> None:
    """CREATE INDEX only when every column exists (post-ALTER). Never raises."""
    try:
        cur = await db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
            (table,),
        )
        if not await cur.fetchone():
            logger.warning(
                "hospital migration skip index %s: table %s does not exist",
                index_name,
                table,
            )
            return
    except Exception as exc:
        logger.warning(
            "hospital migration skip index %s: could not verify table %s (%s)",
            index_name,
            table,
            exc,
        )
        return

    colset = await _hospital_column_names(db, table)
    missing = [c for c in columns if c not in colset]
    if missing:
        logger.warning(
            "hospital migration skip index %s on %s: missing columns %s",
            index_name,
            table,
            missing,
        )
        return
    try:
        await db.execute(sql)
    except Exception as exc:
        logger.warning(
            "hospital migration CREATE INDEX failed %s: %s",
            index_name,
            exc,
            exc_info=True,
        )


async def _migrate_hospital_schema(db: aiosqlite.Connection) -> None:
    """Apply additive hospital_* schema for existing DBs.

    All hospital_* CREATE INDEX runs here after ALTER, never in the main
    executescript, so old DBs without new columns do not fail startup.

    Manual QA checklist (Hospital Work):
    - init_db on DB that already had pre-57d2c5c hospital_* tables: migration runs,
      ALTER/INDEX failures are logged (warning) and do not stop startup.
    - Empty hospital_projects + legacy codes (his_core/lab_lis/pacs_ris): seed
      real projects (Cloud PBX, Backup Solution, …, Migration DB to AWS).
    - Soft-delete task/issue/other: list_* hides is_active=0; daily report uses
      active rows only.
    - Daily report text: mention from standup_mention; contains Cloud PBX /
      Backup Solution / Migration DB to AWS when seed applied.
    """
    cur = await db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='hospital_projects' LIMIT 1"
    )
    if not await cur.fetchone():
        return

    migrations: list[tuple[str, str, str]] = [
        ("hospital_projects", "description", "TEXT DEFAULT ''"),
        ("hospital_projects", "start_date", "TEXT"),
        ("hospital_projects", "end_date", "TEXT"),
        ("hospital_projects", "due_date", "TEXT"),
        ("hospital_projects", "implementation_date", "TEXT"),
        ("hospital_projects", "next_step", "TEXT DEFAULT ''"),
        ("hospital_projects", "notes", "TEXT DEFAULT ''"),
        ("hospital_projects", "vendor", "TEXT DEFAULT ''"),
        ("hospital_projects", "owner", "TEXT DEFAULT ''"),
        ("hospital_projects", "priority", "TEXT DEFAULT 'Medium'"),
        ("hospital_project_tasks", "details", "TEXT DEFAULT ''"),
        ("hospital_project_tasks", "start_date", "TEXT"),
        ("hospital_project_tasks", "end_date", "TEXT"),
        ("hospital_project_tasks", "due_date", "TEXT"),
        ("hospital_project_tasks", "notes", "TEXT DEFAULT ''"),
        ("hospital_project_tasks", "is_active", "INTEGER DEFAULT 1"),
        ("hospital_issues", "system_name", "TEXT DEFAULT ''"),
        ("hospital_issues", "impact", "TEXT DEFAULT ''"),
        ("hospital_issues", "priority", "TEXT DEFAULT 'Medium'"),
        ("hospital_issues", "start_date", "TEXT"),
        ("hospital_issues", "end_date", "TEXT"),
        ("hospital_issues", "due_date", "TEXT"),
        ("hospital_issues", "what_done", "TEXT DEFAULT ''"),
        ("hospital_issues", "next_step", "TEXT DEFAULT ''"),
        ("hospital_issues", "notes", "TEXT DEFAULT ''"),
        ("hospital_issues", "is_active", "INTEGER DEFAULT 1"),
        ("hospital_other_tasks", "details", "TEXT DEFAULT ''"),
        ("hospital_other_tasks", "priority", "TEXT DEFAULT 'Medium'"),
        ("hospital_other_tasks", "requester", "TEXT DEFAULT ''"),
        ("hospital_other_tasks", "start_date", "TEXT"),
        ("hospital_other_tasks", "end_date", "TEXT"),
        ("hospital_other_tasks", "due_date", "TEXT"),
        ("hospital_other_tasks", "related_project_id", "INTEGER"),
        ("hospital_other_tasks", "is_active", "INTEGER DEFAULT 1"),
    ]
    for table, column, ddl in migrations:
        await _hospital_add_column_if_missing(db, table, column, ddl)

    index_defs: list[tuple[str, str, tuple[str, ...], str]] = [
        (
            "idx_hospital_project_tasks_pid",
            "hospital_project_tasks",
            ("project_id", "sort_order"),
            "CREATE INDEX IF NOT EXISTS idx_hospital_project_tasks_pid "
            "ON hospital_project_tasks(project_id, sort_order)",
        ),
        (
            "idx_hospital_issues_pid",
            "hospital_issues",
            ("project_id", "status"),
            "CREATE INDEX IF NOT EXISTS idx_hospital_issues_pid "
            "ON hospital_issues(project_id, status)",
        ),
        (
            "idx_hospital_other_sort",
            "hospital_other_tasks",
            ("sort_order",),
            "CREATE INDEX IF NOT EXISTS idx_hospital_other_sort "
            "ON hospital_other_tasks(sort_order)",
        ),
        (
            "idx_hospital_projects_active_sort",
            "hospital_projects",
            ("is_active", "sort_order"),
            "CREATE INDEX IF NOT EXISTS idx_hospital_projects_active_sort "
            "ON hospital_projects(is_active, sort_order)",
        ),
        (
            "idx_hospital_tasks_pid_active_sort",
            "hospital_project_tasks",
            ("project_id", "is_active", "sort_order"),
            "CREATE INDEX IF NOT EXISTS idx_hospital_tasks_pid_active_sort "
            "ON hospital_project_tasks(project_id, is_active, sort_order)",
        ),
        (
            "idx_hospital_issues_active_status_prio_due",
            "hospital_issues",
            ("is_active", "status", "priority", "due_date"),
            "CREATE INDEX IF NOT EXISTS idx_hospital_issues_active_status_prio_due "
            "ON hospital_issues(is_active, status, priority, due_date)",
        ),
        (
            "idx_hospital_other_active_status_due",
            "hospital_other_tasks",
            ("is_active", "status", "due_date"),
            "CREATE INDEX IF NOT EXISTS idx_hospital_other_active_status_due "
            "ON hospital_other_tasks(is_active, status, due_date)",
        ),
    ]
    for index_name, table, cols, sql in index_defs:
        await _hospital_create_index_if_columns_exist(db, index_name, table, cols, sql)


async def _seed_hospital_work_phase1(db: aiosqlite.Connection) -> None:
    cur = await db.execute("SELECT code FROM hospital_projects")
    codes = {str(r["code"]) for r in await cur.fetchall()}
    legacy = {"his_core", "lab_lis", "pacs_ris"}
    if codes and codes <= legacy:
        await db.executescript(
            """
            DELETE FROM hospital_issues;
            DELETE FROM hospital_project_tasks;
            DELETE FROM hospital_other_tasks;
            DELETE FROM hospital_projects;
            """
        )
        codes = set()
    if codes:
        return

    def ins_project(
        name: str,
        code: str,
        status: str,
        pct: int,
        current_status: str,
        next_step: str,
        implementation_date: str,
        sort_order: int,
        description: str = "",
        priority: str = "Medium",
    ) -> tuple:
        return (
            name,
            code,
            status,
            pct,
            current_status,
            sort_order,
            description,
            next_step,
            implementation_date,
            priority,
        )

    projects: list[tuple] = [
        ins_project(
            "Cloud Contact Center และ Cloud PBX",
            "cloud_cc_pbx",
            "In Progress",
            5,
            "PBX เอาระบบขึ้นภายใน May 2026 เปลี่ยนระบบจาก PABX เป็น PBX Phone System บน Cloud",
            "ตาม Scope of Work + แจ้งจัดซื้อออก PO",
            "กรกฎาคม 2569",
            1,
        ),
        ins_project(
            "Backup Solution",
            "backup_solution",
            "In Progress",
            85,
            "ติดตั้งเสร็จ เหลือ Backup to AWS + Training + Document",
            "FWD meeting ให้ทีม YIP config backup to AWS + ทำ Document",
            "",
            2,
        ),
        ins_project(
            "จัดหา Storage",
            "procure_storage",
            "In Progress",
            20,
            "อัปเดตราคาใหม่ เครื่องมือแพทย์ DB DICOM file network 25gb",
            "ตามใบเสนอราคา + เสนอ solution",
            "",
            3,
        ),
        ins_project(
            "Host VM Resource",
            "host_vm_resource",
            "In Progress",
            30,
            "นัดสรุป solution",
            "ตามใบเสนอราคา + นัด final solution",
            "",
            4,
        ),
        ins_project(
            "Improvement New Network",
            "improve_network",
            "In Progress",
            78,
            "อยู่ในขั้นวางแผน",
            "วางแผน infrastructure ใหม่",
            "Dec 2026",
            5,
        ),
    ]

    task_rows: list[tuple[int, str, str, str, int, str, str]] = []

    for (
        name,
        code,
        status,
        pct,
        current_status,
        sort_order,
        description,
        next_step,
        implementation_date,
        priority,
    ) in projects:
        await db.execute(
            """
            INSERT INTO hospital_projects (
                name, code, status, percent_complete, current_status, sort_order,
                description, next_step, implementation_date, priority, is_active
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
            """,
            (
                name,
                code,
                status,
                pct,
                current_status,
                sort_order,
                description,
                next_step,
                implementation_date,
                priority,
            ),
        )
        curp = await db.execute("SELECT last_insert_rowid() AS id")
        pid = int((await curp.fetchone())["id"])

        if code == "cloud_cc_pbx":
            task_rows += [
                (pid, "ทบทวน TOR และ BOQ (ฉบับปรับปรุง)", "open", "", 1, "", ""),
                (pid, "สรุป Proposal และคัดเลือก Vendor Phase 1", "open", "", 2, "", ""),
                (pid, "แผนรองรับ Scalability & Integration", "open", "", 3, "", ""),
            ]
        elif code == "backup_solution":
            task_rows += [
                (pid, "Meeting config backup to AWS", "open", "", 1, "", ""),
                (pid, "ทำ Document ให้เสร็จ", "open", "", 2, "", ""),
            ]
        elif code == "procure_storage":
            task_rows += [
                (pid, "อัปเดตราคา", "open", "", 1, "", ""),
                (pid, "เสนอ solution", "open", "", 2, "", ""),
            ]
        elif code == "host_vm_resource":
            task_rows += [
                (pid, "นัดสรุป solution", "open", "", 1, "", ""),
            ]
        elif code == "improve_network":
            task_rows += [
                (pid, "วางแผน infrastructure ใหม่", "open", "", 1, "", ""),
            ]

    for pid, title, tstat, due_hint, tsort, details, notes in task_rows:
        await db.execute(
            """
            INSERT INTO hospital_project_tasks (
                project_id, title, status, due_hint, sort_order, details, notes, is_active
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 1)
            """,
            (pid, title, tstat, due_hint, tsort, details, notes),
        )

    await db.execute(
        """
        INSERT INTO hospital_other_tasks (
            title, status, notes, sort_order, details, priority, is_active
        ) VALUES (?, 'open', '', 1, ?, 'Medium', 1)
        """,
        (
            "Migration DB to AWS",
            "เช็ค traffic network ว่า DB ขึ้น AWS infra จะพอไหม เตรียม network/server infra",
        ),
    )


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
        await db.execute("PRAGMA foreign_keys=ON")
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

            CREATE TABLE IF NOT EXISTS otp_audit_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                event_type TEXT NOT NULL,
                path TEXT,
                method TEXT,
                client_ip TEXT,
                user_agent TEXT,
                referer TEXT,
                session_present INTEGER DEFAULT 0,
                session_valid INTEGER,
                auth_header_present INTEGER DEFAULT 0,
                reason TEXT,
                metadata_json TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_otp_audit_created ON otp_audit_logs(created_at);
            CREATE INDEX IF NOT EXISTS idx_otp_audit_event_created ON otp_audit_logs(event_type, created_at);
            CREATE INDEX IF NOT EXISTS idx_otp_audit_path_created ON otp_audit_logs(path, created_at);

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

            CREATE TABLE IF NOT EXISTS hospital_projects (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                code TEXT UNIQUE NOT NULL,
                status TEXT DEFAULT 'In Progress',
                percent_complete INTEGER DEFAULT 0,
                current_status TEXT DEFAULT '',
                sort_order INTEGER DEFAULT 0,
                is_active INTEGER DEFAULT 1,
                description TEXT DEFAULT '',
                start_date TEXT,
                end_date TEXT,
                due_date TEXT,
                implementation_date TEXT,
                next_step TEXT DEFAULT '',
                notes TEXT DEFAULT '',
                vendor TEXT DEFAULT '',
                owner TEXT DEFAULT '',
                priority TEXT DEFAULT 'Medium',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS hospital_project_tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id INTEGER NOT NULL,
                title TEXT NOT NULL,
                status TEXT DEFAULT 'open',
                due_hint TEXT DEFAULT '',
                sort_order INTEGER DEFAULT 0,
                details TEXT DEFAULT '',
                start_date TEXT,
                end_date TEXT,
                due_date TEXT,
                notes TEXT DEFAULT '',
                is_active INTEGER DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (project_id) REFERENCES hospital_projects(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS hospital_issues (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id INTEGER,
                title TEXT NOT NULL,
                severity TEXT DEFAULT 'medium',
                status TEXT DEFAULT 'open',
                details TEXT DEFAULT '',
                system_name TEXT DEFAULT '',
                impact TEXT DEFAULT '',
                priority TEXT DEFAULT 'Medium',
                start_date TEXT,
                end_date TEXT,
                due_date TEXT,
                what_done TEXT DEFAULT '',
                next_step TEXT DEFAULT '',
                notes TEXT DEFAULT '',
                is_active INTEGER DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (project_id) REFERENCES hospital_projects(id) ON DELETE SET NULL
            );

            CREATE TABLE IF NOT EXISTS hospital_other_tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                status TEXT DEFAULT 'open',
                notes TEXT DEFAULT '',
                sort_order INTEGER DEFAULT 0,
                details TEXT DEFAULT '',
                priority TEXT DEFAULT 'Medium',
                requester TEXT DEFAULT '',
                start_date TEXT,
                end_date TEXT,
                due_date TEXT,
                related_project_id INTEGER,
                is_active INTEGER DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (related_project_id) REFERENCES hospital_projects(id) ON DELETE SET NULL
            );

            CREATE INDEX IF NOT EXISTS idx_agent_events_agent
                ON agent_events(agent_name, result, created_at);
            CREATE INDEX IF NOT EXISTS idx_agent_events_tags
                ON agent_events(tags);
            CREATE UNIQUE INDEX IF NOT EXISTS idx_news_url_date
                ON news_items(url, date(datetime(fetched_at, '+7 hours')));
        """)
        await _migrate_hospital_schema(db)
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
        message_migrations: list[tuple[str, str]] = [
            ("conversation_id", "TEXT"),
            ("intent", "TEXT"),
            ("model_used", "TEXT"),
            ("route_json", "TEXT"),
            ("context_snapshot", "TEXT"),
            ("external_used", "INTEGER DEFAULT 0"),
            ("trace_id", "TEXT"),
            ("source", "TEXT DEFAULT 'telegram'"),
            ("project_id", "INTEGER REFERENCES projects(id)"),
        ]
        for column, ddl in message_migrations:
            await _add_column_if_missing(db, "messages", column, ddl)
        await db.executescript("""
            CREATE TABLE IF NOT EXISTS conversations (
                id TEXT PRIMARY KEY,
                source TEXT DEFAULT 'telegram',
                external_chat_id TEXT,
                project_id INTEGER,
                title TEXT DEFAULT '',
                last_intent TEXT DEFAULT '',
                last_model TEXT DEFAULT '',
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS tool_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                trace_id TEXT,
                conversation_id TEXT,
                tool_name TEXT NOT NULL,
                tool_input_json TEXT,
                tool_output_preview TEXT,
                success INTEGER DEFAULT 1,
                error TEXT,
                duration_ms INTEGER DEFAULT 0,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS code_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                trace_id TEXT,
                request_id TEXT,
                action TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                files_json TEXT,
                tests_json TEXT,
                deploy_json TEXT,
                error TEXT,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
            );

            CREATE INDEX IF NOT EXISTS idx_messages_trace_id ON messages(trace_id);
            CREATE INDEX IF NOT EXISTS idx_messages_conversation_created ON messages(conversation_id, created_at);
            CREATE INDEX IF NOT EXISTS idx_messages_project_created ON messages(project_id, created_at);
            CREATE INDEX IF NOT EXISTS idx_conversations_source_external ON conversations(source, external_chat_id);
            CREATE INDEX IF NOT EXISTS idx_tool_runs_trace_id ON tool_runs(trace_id);
            CREATE INDEX IF NOT EXISTS idx_code_runs_trace_id ON code_runs(trace_id);

            CREATE TABLE IF NOT EXISTS project_artifacts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id INTEGER,
                project_slug TEXT,
                source TEXT NOT NULL,
                external_id TEXT,
                artifact_type TEXT NOT NULL,
                title TEXT DEFAULT '',
                summary TEXT DEFAULT '',
                payload_json TEXT,
                tags TEXT DEFAULT '[]',
                event_id INTEGER,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
            );

            CREATE INDEX IF NOT EXISTS idx_project_artifacts_project_slug_created
                ON project_artifacts(project_slug, created_at);
            CREATE INDEX IF NOT EXISTS idx_project_artifacts_source_external
                ON project_artifacts(source, external_id);
            CREATE INDEX IF NOT EXISTS idx_project_artifacts_type_created
                ON project_artifacts(artifact_type, created_at);
            CREATE INDEX IF NOT EXISTS idx_project_artifacts_event_id
                ON project_artifacts(event_id);
        """)
        try:
            await db.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_project_artifacts_event_unique
                ON project_artifacts(event_id)
                WHERE event_id IS NOT NULL
                """
            )
        except Exception as exc:
            logger.warning(
                "partial unique index idx_project_artifacts_event_unique not available: %s",
                exc,
            )
        try:
            await db.execute("""
                CREATE VIRTUAL TABLE IF NOT EXISTS local_knowledge_fts USING fts5(
                    source,
                    source_id,
                    title,
                    content,
                    tags,
                    created_at
                );
            """)
        except Exception as exc:
            logger.warning("optional FTS5 table local_knowledge_fts not available: %s", exc)
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
        await _seed_hospital_work_phase1(db)
        await _add_column_if_missing(db, "code_runs", "lesson_learned", "TEXT")
        await db.commit()
    await _ensure_fts_backfill()


_FTS_BACKFILL_KEY = "fts_memories_backfilled"


def _fts_tags(project_id: int | None, extra: str = "") -> str:
    parts = [str(extra or "").strip()]
    if project_id is not None:
        parts.append(f"project_id:{project_id}")
    return ";".join(p for p in parts if p)


async def index_message_to_fts(
    *,
    source_table: str,
    source_id: str,
    project_id: int | None,
    title: str,
    content: str,
    tags: str = "",
) -> None:
    """Index one row into local_knowledge_fts (no-op if FTS unavailable)."""
    try:
        async with get_db() as db:
            await db.execute(
                """
                INSERT INTO local_knowledge_fts(
                    source, source_id, title, content, tags, created_at
                )
                VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                """,
                (
                    str(source_table or "")[:64],
                    str(source_id or "")[:128],
                    str(title or "")[:500],
                    str(content or "")[:2000],
                    _fts_tags(project_id, tags),
                ),
            )
            await db.commit()
    except Exception as exc:
        logger.warning("index_message_to_fts skipped: %s", exc)


async def populate_fts_from_memories(db: aiosqlite.Connection) -> int:
    """Backfill FTS from long_term_memories (idempotent per row)."""
    count = 0
    try:
        cur = await db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='local_knowledge_fts'"
        )
        if not await cur.fetchone():
            return 0
        await db.execute(
            "DELETE FROM local_knowledge_fts WHERE source = 'long_term_memories'"
        )
        cur = await db.execute(
            """
            SELECT id, content, created_at
            FROM long_term_memories
            WHERE TRIM(content) <> ''
            """
        )
        rows = await cur.fetchall()
        for row in rows:
            await db.execute(
                """
                INSERT INTO local_knowledge_fts(
                    source, source_id, title, content, tags, created_at
                )
                VALUES (?, ?, '', ?, '', ?)
                """,
                (
                    "long_term_memories",
                    str(row["id"]),
                    str(row["content"] or "")[:2000],
                    str(row["created_at"] or ""),
                ),
            )
            count += 1
    except Exception as exc:
        logger.warning("populate_fts_from_memories failed: %s", exc)
        return 0
    return count


async def _ensure_fts_backfill() -> None:
    try:
        async with get_db() as db:
            cur = await db.execute(
                "SELECT value FROM memories WHERE key = ? LIMIT 1",
                (_FTS_BACKFILL_KEY,),
            )
            row = await cur.fetchone()
            if row and str(row["value"] or "").strip() == "1":
                return
            inserted = await populate_fts_from_memories(db)
            await db.execute(
                """
                INSERT INTO memories (key, value, tag)
                VALUES (?, ?, 'system')
                ON CONFLICT(key) DO UPDATE SET
                    value = excluded.value,
                    tag = excluded.tag,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (_FTS_BACKFILL_KEY, str(inserted)),
            )
            await db.commit()
    except Exception as exc:
        logger.warning("fts backfill skipped: %s", exc)


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
            ("hospital_projects", "hospital_projects"),
            ("hospital_project_tasks", "hospital_project_tasks"),
            ("hospital_issues", "hospital_issues"),
            ("hospital_other_tasks", "hospital_other_tasks"),
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
