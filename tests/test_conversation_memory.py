"""Tests for cross-model conversation memory via conversation_id."""

import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.core.ai_gateway import (
    get_or_create_conversation,
    get_recent_history,
    run_ai,
    save_gateway_message,
)
from app.core.database import get_db, init_db


class ConversationMemoryTest(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._db_path = os.path.join(self._tmpdir.name, "test_ener.db")
        self._settings_patch = patch(
            "app.core.database.settings.database_path",
            self._db_path,
        )
        self._db_path_patch = patch(
            "app.core.database.DB_PATH",
            Path(self._db_path),
        )
        self._settings_patch.start()
        self._db_path_patch.start()
        asyncio.run(init_db())

    def tearDown(self):
        self._db_path_patch.stop()
        self._settings_patch.stop()
        self._tmpdir.cleanup()

    def test_get_or_create_conversation_reuses_same_id(self):
        async def _run():
            first = await get_or_create_conversation(
                source="telegram",
                external_chat_id="chat-123",
            )
            second = await get_or_create_conversation(
                source="telegram",
                external_chat_id="chat-123",
            )
            return first, second

        first, second = asyncio.run(_run())
        self.assertEqual(first, second)

    def test_get_recent_history_scopes_by_conversation_id(self):
        async def _run():
            conv_a = await get_or_create_conversation(
                source="telegram",
                external_chat_id="chat-a",
            )
            conv_b = await get_or_create_conversation(
                source="telegram",
                external_chat_id="chat-b",
            )
            await save_gateway_message(
                external_chat_id="chat-a",
                conversation_id=conv_a,
                role="user",
                content="hello from A",
                project_id=None,
                source="telegram",
                intent="chat",
                model_used="groq",
                route={},
                context_snapshot="",
                external_used=0,
                trace_id="trace-a",
            )
            await save_gateway_message(
                external_chat_id="chat-a",
                conversation_id=conv_a,
                role="assistant",
                content="reply A",
                project_id=None,
                source="telegram",
                intent="chat",
                model_used="groq",
                route={},
                context_snapshot="",
                external_used=0,
                trace_id="trace-a",
            )
            await save_gateway_message(
                external_chat_id="chat-b",
                conversation_id=conv_b,
                role="user",
                content="hello from B",
                project_id=None,
                source="telegram",
                intent="chat",
                model_used="haiku",
                route={},
                context_snapshot="",
                external_used=0,
                trace_id="trace-b",
            )
            return conv_a, await get_recent_history(conversation_id=conv_a, limit=20)

        conv_a, history = asyncio.run(_run())
        self.assertEqual(len(history), 2)
        self.assertEqual(history[0]["content"], "hello from A")
        self.assertEqual(history[1]["content"], "reply A")

    def test_run_ai_saves_once_per_turn_with_conversation_id(self):
        async def _run():
            with patch(
                "app.core.ai_gateway.run_pipeline",
                return_value=("model B summary", {"model_used": "haiku"}),
            ):
                await run_ai(
                    source="telegram",
                    external_chat_id="chat-switch",
                    text="message one",
                )
                await run_ai(
                    source="telegram",
                    external_chat_id="chat-switch",
                    text="สรุปสิ่งที่เราคุยมา",
                )
            async with get_db() as db:
                cur = await db.execute(
                    """
                    SELECT COUNT(*) AS cnt
                    FROM messages
                    WHERE chat_id = ? AND conversation_id IS NOT NULL
                    """,
                    ("chat-switch",),
                )
                row = await cur.fetchone()
                cur2 = await db.execute(
                    """
                    SELECT COUNT(*) AS cnt
                    FROM messages
                    WHERE chat_id = ? AND conversation_id IS NULL
                    """,
                    ("chat-switch",),
                )
                bare = await cur2.fetchone()
                cur3 = await db.execute(
                    "SELECT COUNT(DISTINCT conversation_id) AS cnt FROM messages WHERE chat_id = ?",
                    ("chat-switch",),
                )
                convs = await cur3.fetchone()
                return int(row["cnt"]), int(bare["cnt"]), int(convs["cnt"])

        with_conv, without_conv, distinct_convs = asyncio.run(_run())
        self.assertEqual(with_conv, 4)
        self.assertEqual(without_conv, 0)
        self.assertEqual(distinct_convs, 1)


if __name__ == "__main__":
    unittest.main()
