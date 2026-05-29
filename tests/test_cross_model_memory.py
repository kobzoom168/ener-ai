"""
Automated cross-model conversation memory tests.
Simulates multi-turn chat across model switches without calling real AI APIs.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.core.ai_gateway import get_or_create_conversation, get_recent_history, run_ai
from app.core.database import get_db, init_db, set_config


class CrossModelMemoryTestBase(unittest.TestCase):
  """Shared tempfile DB and helpers for cross-model memory scenarios."""

  chat_id = "cross-model-test-chat"

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

  async def _switch_model(self, model: str) -> None:
    await set_config("active_model", model)

  async def _run_turn(
    self,
    text: str,
    *,
    reply: str,
    pipeline_model: str,
    chat_id: str | None = None,
    capture_history: list | None = None,
  ) -> dict:
    cid = chat_id or self.chat_id

    async def _fake_pipeline(
      _text: str,
      history: list,
      _system: str,
      route: dict | None = None,
    ):
      if capture_history is not None:
        capture_history.append(list(history))
      return (reply, {"model_used": pipeline_model})

    with patch("app.core.ai_gateway.run_pipeline", side_effect=_fake_pipeline):
      return await run_ai(
        source="telegram",
        external_chat_id=cid,
        text=text,
      )

  async def _message_counts(self, chat_id: str | None = None) -> tuple[int, int, int]:
    cid = chat_id or self.chat_id
    async with get_db() as db:
      with_conv = await (
        await db.execute(
          """
          SELECT COUNT(*) AS cnt FROM messages
          WHERE chat_id = ? AND conversation_id IS NOT NULL
          """,
          (cid,),
        )
      ).fetchone()
      without_conv = await (
        await db.execute(
          """
          SELECT COUNT(*) AS cnt FROM messages
          WHERE chat_id = ? AND conversation_id IS NULL
          """,
          (cid,),
        )
      ).fetchone()
      distinct = await (
        await db.execute(
          """
          SELECT COUNT(DISTINCT conversation_id) AS cnt
          FROM messages WHERE chat_id = ?
          """,
          (cid,),
        )
      ).fetchone()
    return (
      int(with_conv["cnt"]),
      int(without_conv["cnt"]),
      int(distinct["cnt"]),
    )

  async def _models_used_in_order(self, chat_id: str | None = None) -> list[str]:
    cid = chat_id or self.chat_id
    async with get_db() as db:
      cur = await db.execute(
        """
        SELECT model_used FROM messages
        WHERE chat_id = ?
        ORDER BY id ASC
        """,
        (cid,),
      )
      rows = await cur.fetchall()
    return [str(r["model_used"] or "") for r in rows]


class Test1_PersonalInfoSurvivesModelSwitch(CrossModelMemoryTestBase):
  def test_personal_info_in_history_after_model_switch(self):
    async def _run():
      captured: list[list] = []
      await self._switch_model("groq")
      await self._run_turn(
        "ชื่อฉันคือกบ อายุ 32",
        reply="รับทราบครับกบ",
        pipeline_model="groq",
      )
      await self._switch_model("haiku")
      await self._run_turn(
        "ฉันชื่ออะไร",
        reply="คุณชื่อกบครับ",
        pipeline_model="haiku",
        capture_history=captured,
      )
      conv_id = await get_or_create_conversation(
        source="telegram",
        external_chat_id=self.chat_id,
      )
      history = await get_recent_history(conversation_id=conv_id, limit=20)
      return captured, history

    captured, history = asyncio.run(_run())
    self.assertTrue(captured, "pipeline should receive history on turn 2")
    turn2_history = captured[-1]
    combined = " ".join(m["content"] for m in turn2_history)
    self.assertIn("กบ", combined)
    full_history_text = " ".join(m["content"] for m in history)
    self.assertIn("กบ", full_history_text)


class Test2_CodeLessonPersistsAcrossModel(CrossModelMemoryTestBase):
  def test_code_lesson_in_history_after_gemini_switch(self):
    async def _run():
      captured: list[list] = []
      await self._switch_model("groq")
      await self._run_turn(
        "factorial ต้องเช็ค type ก่อน",
        reply="เข้าใจครับ",
        pipeline_model="groq",
      )
      await self._run_turn(
        "จำไว้ว่า factorial ต้องเช็ค type",
        reply="บันทึกแล้วครับ",
        pipeline_model="groq",
      )
      await self._switch_model("gemini")
      await self._run_turn(
        "สรุปบทเรียนที่เพิ่งคุย",
        reply="factorial ต้องเช็ค type ก่อนครับ",
        pipeline_model="gemini",
        capture_history=captured,
      )
      return captured

    captured = asyncio.run(_run())
    self.assertTrue(captured)
    combined = " ".join(m["content"] for m in captured[-1])
    self.assertIn("factorial", combined.lower())
    self.assertIn("type", combined.lower())


class Test3_NoDuplicateMessages(CrossModelMemoryTestBase):
  def test_no_bare_messages_after_three_model_switches(self):
    async def _run():
      await self._switch_model("groq")
      await self._run_turn("ข้อความที่หนึ่ง", reply="ตอบหนึ่ง", pipeline_model="groq")
      await self._switch_model("haiku")
      await self._run_turn("ข้อความที่สอง", reply="ตอบสอง", pipeline_model="haiku")
      await self._switch_model("gemini")
      await self._run_turn("ข้อความที่สาม", reply="ตอบสาม", pipeline_model="gemini")
      return await self._message_counts()

    with_conv, without_conv, distinct = asyncio.run(_run())
    self.assertEqual(with_conv, 6)
    self.assertEqual(without_conv, 0)
    self.assertEqual(distinct, 1)


class Test4_ConversationIdPersists(CrossModelMemoryTestBase):
  def test_same_conversation_id_across_model_switches(self):
    async def _run():
      ids = []
      for model in ("groq", "haiku", "gemini", "groq", "haiku"):
        await self._switch_model(model)
        ids.append(
          await get_or_create_conversation(
            source="telegram",
            external_chat_id=self.chat_id,
          )
        )
      return ids

    ids = asyncio.run(_run())
    self.assertEqual(len(set(ids)), 1)
    self.assertTrue(ids[0])


class Test5_ModelUsedRecordedCorrectly(CrossModelMemoryTestBase):
  def test_model_used_per_turn_in_database(self):
    async def _run():
      await self._switch_model("groq")
      await self._run_turn("เทิร์นแรก", reply="ตอบแรก", pipeline_model="groq")
      await self._switch_model("haiku")
      await self._run_turn("เทิร์นสอง", reply="ตอบสอง", pipeline_model="haiku")
      return await self._models_used_in_order()

    models = asyncio.run(_run())
    self.assertEqual(models, ["groq", "groq", "haiku", "haiku"])


if __name__ == "__main__":
  unittest.main()
