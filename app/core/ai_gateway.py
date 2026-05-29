from __future__ import annotations

import json
import logging
import time
import uuid

from app.core.ai import _VALID_MODELS, get_active_model
from app.core.context_builder import build_context_v2
from app.core.database import get_db, index_message_to_fts
from app.core.event_log import log_event
from app.core.policy import BASE_SYSTEM_PROMPT
from app.core.reasoning_pipeline import get_routing_config, route_fast, run_pipeline
from app.core.trace_context import reset_trace_context, set_trace_context

logger = logging.getLogger(__name__)

# Domains that must keep router-selected model (capabilities / tool loops).
_KEEP_ROUTER_MODEL = frozenset({"vision", "code_agent", "image_analysis"})

def _intent_from_route(route: dict) -> str:
    for key in ("intent", "domain", "reason"):
        val = str(route.get(key, "")).strip()
        if val:
            return val
    return "chat"


def _preview(value: str, limit: int = 220) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


async def get_or_create_conversation(
    *,
    source: str,
    external_chat_id: str,
    project_id: int | None = None,
) -> str:
    src = str(source or "telegram").strip() or "telegram"
    chat_id = str(external_chat_id or "").strip() or "unknown"
    async with get_db() as db:
        cur = await db.execute(
            """
            SELECT id
            FROM conversations
            WHERE source = ? AND external_chat_id = ?
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (src, chat_id),
        )
        row = await cur.fetchone()
        if row:
            conversation_id = str(row["id"])
            await db.execute(
                """
                UPDATE conversations
                SET project_id = COALESCE(?, project_id),
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (project_id, conversation_id),
            )
            await db.commit()
            return conversation_id

        conversation_id = uuid.uuid4().hex
        await db.execute(
            """
            INSERT INTO conversations (
                id, source, external_chat_id, project_id, title, last_intent, last_model
            )
            VALUES (?, ?, ?, ?, '', '', '')
            """,
            (conversation_id, src, chat_id, project_id),
        )
        await db.commit()
        return conversation_id


async def get_recent_history(
    *,
    conversation_id: str,
    limit: int = 20,
) -> list[dict]:
    async with get_db() as db:
        cur = await db.execute(
            """
            SELECT role, content
            FROM messages
            WHERE conversation_id = ? AND role IN ('user', 'assistant')
            ORDER BY id DESC
            LIMIT ?
            """,
            (conversation_id, max(1, int(limit))),
        )
        rows = await cur.fetchall()
    return [{"role": str(r["role"]), "content": str(r["content"])} for r in reversed(rows)]


async def _log_chat_message_saved(external_chat_id: str, conversation_id: str) -> None:
    async with get_db() as db:
        await db.execute(
            "INSERT INTO audit_logs (action, details) VALUES (?, ?)",
            (
                "chat_message_saved",
                f"chat_id={external_chat_id} conversation_id={conversation_id}",
            ),
        )
        await db.commit()


async def save_gateway_message(
    *,
    external_chat_id: str,
    conversation_id: str,
    role: str,
    content: str,
    project_id: int | None,
    source: str,
    intent: str,
    model_used: str,
    route: dict,
    context_snapshot: str,
    external_used: int,
    trace_id: str,
) -> int | None:
    message_id: int | None = None
    async with get_db() as db:
        cur = await db.execute(
            """
            INSERT INTO messages (
                chat_id, conversation_id, role, content, project_id, source, intent,
                model_used, route_json, context_snapshot, external_used, trace_id
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(external_chat_id),
                str(conversation_id),
                str(role),
                str(content),
                project_id,
                str(source),
                str(intent),
                str(model_used),
                json.dumps(route or {}, ensure_ascii=False),
                str(context_snapshot or ""),
                int(external_used),
                str(trace_id),
            ),
        )
        message_id = cur.lastrowid
        await db.commit()
    if message_id:
        await index_message_to_fts(
            source_table="messages",
            source_id=str(message_id),
            project_id=project_id,
            title="",
            content=content,
            tags=intent,
        )
    return message_id


async def run_ai(
    *,
    source: str,
    external_chat_id: str,
    text: str,
    project_id: int | None = None,
    system_prompt: str | None = None,
    history: list[dict] | None = None,
) -> dict:
    started = time.time()
    trace_id = uuid.uuid4().hex[:12]
    source = str(source or "telegram").strip() or "telegram"
    external_chat_id = str(external_chat_id or "").strip() or "unknown"

    conversation_id = await get_or_create_conversation(
        source=source,
        external_chat_id=external_chat_id,
        project_id=project_id,
    )
    if history is None:
        history = await get_recent_history(conversation_id=conversation_id, limit=20)

    routing = await get_routing_config()
    route = route_fast(text, routing=routing)
    intent = _intent_from_route(route)
    route_model = str(route.get("model", "groq") or "groq")

    active_model = await get_active_model()
    if (
        active_model
        and active_model != "auto"
        and active_model in _VALID_MODELS
        and intent not in _KEEP_ROUTER_MODEL
    ):
        route = {**route, "model": active_model}
        route_model = active_model

    context = await build_context_v2(
        text=text,
        route=route,
        conversation_id=conversation_id,
        chat_id=external_chat_id,
        project_id=project_id,
    )
    context_text = str(context.get("text") or "")
    context_summary = str(context.get("summary") or "")
    external_used = 0
    enhanced_system = (system_prompt or BASE_SYSTEM_PROMPT) + "\n\n" + context_text

    tokens = set_trace_context(
        trace_id=trace_id,
        conversation_id=conversation_id,
        source=source,
        project_id=project_id,
    )
    try:
        reply, pipeline_meta = await run_pipeline(
            text, history, enhanced_system, route=route
        )
    except Exception as exc:
        try:
            await log_event(
                agent_name="AIGateway",
                event_type="task_failed",
                summary=f"run_ai fail: {_preview(text, 80)}",
                tags=["ai-gateway", "error", source],
                context=json.dumps(
                    {
                        "trace_id": trace_id,
                        "conversation_id": conversation_id,
                        "route": route,
                    },
                    ensure_ascii=False,
                ),
                result="failure",
                learned=str(exc)[:240],
            )
        except Exception:
            logger.exception("failed to log run_ai failure")
        raise
    finally:
        reset_trace_context(tokens)

    model_used = str(pipeline_meta.get("model_used") or route_model)
    snapshot = _preview(context_summary or context_text, 1200)

    await save_gateway_message(
        external_chat_id=external_chat_id,
        conversation_id=conversation_id,
        role="user",
        content=text,
        project_id=project_id,
        source=source,
        intent=intent,
        model_used=route_model,
        route=route,
        context_snapshot=snapshot,
        external_used=external_used,
        trace_id=trace_id,
    )
    await save_gateway_message(
        external_chat_id=external_chat_id,
        conversation_id=conversation_id,
        role="assistant",
        content=reply,
        project_id=project_id,
        source=source,
        intent=intent,
        model_used=model_used,
        route=route,
        context_snapshot=snapshot,
        external_used=external_used,
        trace_id=trace_id,
    )
    await _log_chat_message_saved(external_chat_id, conversation_id)

    async with get_db() as db:
        await db.execute(
            """
            UPDATE conversations
            SET last_intent = ?, last_model = ?, updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (intent, model_used, conversation_id),
        )
        await db.commit()

    elapsed_ms = int((time.time() - started) * 1000)
    return {
        "reply": reply,
        "trace_id": trace_id,
        "conversation_id": conversation_id,
        "route": route,
        "context_summary": context_summary,
        "model_used": model_used,
        "elapsed_ms": elapsed_ms,
    }


async def get_recent_ai_traces(limit: int = 50) -> list[dict]:
    lim = max(1, min(200, int(limit)))
    async with get_db() as db:
        trace_cur = await db.execute(
            """
            SELECT trace_id, MAX(sort_id) AS last_sort_id
            FROM (
                SELECT
                    trace_id, id AS sort_id
                FROM messages
                WHERE trace_id IS NOT NULL AND TRIM(trace_id) <> ''
                UNION ALL
                SELECT trace_id, id AS sort_id
                FROM tool_runs
                WHERE trace_id IS NOT NULL AND TRIM(trace_id) <> ''
                UNION ALL
                SELECT trace_id, id AS sort_id
                FROM code_runs
                WHERE trace_id IS NOT NULL AND TRIM(trace_id) <> ''
            )
            GROUP BY trace_id
            ORDER BY last_sort_id DESC
            LIMIT ?
            """,
            (lim,),
        )
        trace_rows = await trace_cur.fetchall()

    trace_ids = [str(row["trace_id"] or "") for row in trace_rows if row["trace_id"]]
    tool_runs_map: dict[str, list] = {}
    code_runs_map: dict[str, list] = {}
    message_meta: dict[str, dict] = {}
    if trace_ids:
        placeholders = ",".join(["?"] * len(trace_ids))
        async with get_db() as db:
            cur_msg = await db.execute(
                f"""
                SELECT
                    t.trace_id,
                    t.conversation_id,
                    t.source,
                    t.chat_id,
                    t.created_at,
                    t.intent,
                    t.model_used,
                    t.context_snapshot,
                    t.route_json,
                    u.content AS user_content,
                    a.content AS assistant_content
                FROM (
                    SELECT
                        trace_id,
                        MAX(conversation_id) AS conversation_id,
                        MAX(source) AS source,
                        MAX(chat_id) AS chat_id,
                        MAX(created_at) AS created_at,
                        MAX(intent) AS intent,
                        MAX(model_used) AS model_used,
                        MAX(context_snapshot) AS context_snapshot,
                        MAX(route_json) AS route_json
                    FROM messages
                    WHERE trace_id IN ({placeholders})
                    GROUP BY trace_id
                ) t
                LEFT JOIN messages u
                    ON u.trace_id = t.trace_id AND u.role = 'user'
                    AND u.id = (SELECT MAX(id) FROM messages WHERE trace_id = t.trace_id AND role = 'user')
                LEFT JOIN messages a
                    ON a.trace_id = t.trace_id AND a.role = 'assistant'
                    AND a.id = (SELECT MAX(id) FROM messages WHERE trace_id = t.trace_id AND role = 'assistant')
                """,
                tuple(trace_ids),
            )
            for row in await cur_msg.fetchall():
                message_meta[str(row["trace_id"] or "")] = dict(row)
            cur_tools = await db.execute(
                f"""
                SELECT trace_id, tool_name, success, error, duration_ms, created_at, tool_output_preview
                FROM tool_runs
                WHERE trace_id IN ({placeholders})
                ORDER BY id DESC
                """,
                tuple(trace_ids),
            )
            tool_rows = await cur_tools.fetchall()
            for row in tool_rows:
                tid = str(row["trace_id"] or "")
                tool_runs_map.setdefault(tid, []).append(
                    {
                        "tool_name": str(row["tool_name"] or ""),
                        "success": int(row["success"] or 0),
                        "error": _preview(row["error"], 180),
                        "duration_ms": int(row["duration_ms"] or 0),
                        "created_at": str(row["created_at"] or ""),
                        "output_preview": _preview(row["tool_output_preview"], 180),
                    }
                )
            cur_code = await db.execute(
                f"""
                SELECT trace_id, request_id, action, status, error, created_at, updated_at
                FROM code_runs
                WHERE trace_id IN ({placeholders})
                ORDER BY id DESC
                """,
                tuple(trace_ids),
            )
            code_rows = await cur_code.fetchall()
            for row in code_rows:
                tid = str(row["trace_id"] or "")
                code_runs_map.setdefault(tid, []).append(
                    {
                        "request_id": str(row["request_id"] or ""),
                        "action": str(row["action"] or ""),
                        "status": str(row["status"] or ""),
                        "error": _preview(row["error"], 180),
                        "created_at": str(row["created_at"] or ""),
                        "updated_at": str(row["updated_at"] or ""),
                    }
                )

    traces = []
    for trace_id in trace_ids:
        row = message_meta.get(trace_id, {})
        route_json = row.get("route_json")
        try:
            route = json.loads(route_json) if route_json else {}
        except Exception:
            route = {"raw_preview": _preview(route_json, 200)}
        traces.append(
            {
                "trace_id": trace_id,
                "conversation_id": str(row.get("conversation_id") or ""),
                "source": str(row.get("source") or ""),
                "chat_id": str(row.get("chat_id") or ""),
                "created_at": str(row.get("created_at") or ""),
                "user_preview": _preview(row.get("user_content"), 180),
                "assistant_preview": _preview(row.get("assistant_content"), 180),
                "intent": str(row.get("intent") or ""),
                "model_used": str(row.get("model_used") or ""),
                "context_snapshot": _preview(row.get("context_snapshot"), 220),
                "route_json": route,
                "tool_runs": tool_runs_map.get(trace_id, [])[:20],
                "code_runs": code_runs_map.get(trace_id, [])[:20],
            }
        )
    return traces


async def preview_context(
    text: str,
    source: str = "debug",
    external_chat_id: str = "debug",
) -> dict:
    routing = await get_routing_config()
    route = route_fast(text, routing=routing)
    conversation_id = await get_or_create_conversation(
        source=source,
        external_chat_id=external_chat_id,
    )
    context = await build_context_v2(
        text=text,
        route=route,
        conversation_id=conversation_id,
        chat_id=external_chat_id,
    )
    return {
        "trace_id": uuid.uuid4().hex[:12],
        "conversation_id": conversation_id,
        "route": route,
        "context": context,
    }
