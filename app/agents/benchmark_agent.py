import asyncio
import time

from app.core.ai import chat
from app.core.database import get_db

TEST_QUESTIONS = [
    {"id": "it_01", "cat": "🏥 Hospital IT", "q": "สรุป risk 3 ข้อของการย้าย HIS ไป Cloud สำหรับผู้บริหาร"},
    {"id": "it_02", "cat": "🏥 Hospital IT", "q": "draft email ภาษาไทยสั้นๆ แจ้ง vendor PBX ว่า delivery ล่าช้า"},
    {"id": "it_03", "cat": "🏥 Hospital IT", "q": "เปรียบเทียบ On-premise vs Cloud PBX สำหรับโรงพยาบาล 3 ข้อ"},
    {"id": "en_01", "cat": "⚡ Ener Scan", "q": "เขียน TikTok hook 3 แบบสำหรับขายพระสมเด็จ ให้ดึงดูด"},
    {"id": "en_02", "cat": "⚡ Ener Scan", "q": "อธิบายพลังงานของหลวงปู่โต วัดประดู่ฉิมพลี สั้นๆ ให้คนทั่วไปเข้าใจ"},
    {"id": "en_03", "cat": "⚡ Ener Scan", "q": "แนะนำ strategy ขยาย Ener Scan ไป YouTube 3 ข้อ"},
    {"id": "hal_01", "cat": "🔍 Hallucination", "q": "แนะนำร้านขายพระแถวรัตนินพร้อมเบอร์โทรและรีวิว"},
    {"id": "hal_02", "cat": "🔍 Hallucination", "q": "ราคา iPhone 17 Pro ในไทยตอนนี้เท่าไหร่"},
    {"id": "hal_03", "cat": "🔍 Hallucination", "q": "ที่อยู่และเบอร์โทรของ vendor Cisco ในกรุงเทพ"},
    {"id": "ch_01", "cat": "💬 Simple Chat", "q": "สวัสดีครับ วันนี้เป็นยังไงบ้าง"},
    {"id": "ch_02", "cat": "💬 Simple Chat", "q": "Python คืออะไร อธิบายสั้นๆ 2 ประโยค"},
    {"id": "ch_03", "cat": "💬 Simple Chat", "q": "บอกข้อดี 3 อย่างของการตื่นเช้า"},
]

MODELS = ["groq", "gemini", "haiku"]

SYSTEM = (
    "คุณเป็น Ener-AI ผู้ช่วยส่วนตัว ตอบเป็นภาษาไทย กระชับ\n"
    "สำคัญมาก: ถ้าไม่รู้ข้อมูลจริง (เบอร์โทร URL ราคา ชื่อร้าน) "
    "ห้ามแต่งขึ้นเอง ให้บอกตรงๆ ว่าไม่มีข้อมูล"
)


async def _run_single(question: str, model: str) -> dict:
    start = time.time()
    try:
        answer = await chat(
            question,
            system=SYSTEM,
            agent=f"benchmark_{model}",
            messages=[],
            preferred_model=model,
            strict_model=True,
        )
        return {
            "model": model,
            "answer": str(answer),
            "latency_ms": int((time.time() - start) * 1000),
            "error": None,
        }
    except Exception as exc:
        return {
            "model": model,
            "answer": "",
            "latency_ms": -1,
            "error": str(exc)[:200],
        }


async def run_benchmark(question_ids: list[str] | None = None) -> list[dict]:
    questions = TEST_QUESTIONS
    if question_ids:
        questions = [q for q in TEST_QUESTIONS if q["id"] in question_ids]

    results = []
    for question in questions:
        tasks = [_run_single(question["q"], model) for model in MODELS]
        model_results = await asyncio.gather(*tasks)

        saved_results = []
        async with get_db() as db:
            for result in model_results:
                cursor = await db.execute(
                    """
                    INSERT INTO benchmark_results
                        (question_id, category, question, model, answer, latency_ms, error)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        question["id"],
                        question["cat"],
                        question["q"],
                        result["model"],
                        result["answer"],
                        result["latency_ms"],
                        result["error"],
                    ),
                )
                saved_results.append(
                    {
                        "id": int(cursor.lastrowid),
                        **result,
                    }
                )
            await db.commit()

        results.append(
            {
                "question_id": question["id"],
                "category": question["cat"],
                "question": question["q"],
                "results": saved_results,
            }
        )

    return results


async def save_rating(result_id: int, rating: int) -> None:
    async with get_db() as db:
        await db.execute(
            "UPDATE benchmark_results SET rating = ? WHERE id = ?",
            (rating, result_id),
        )
        await db.commit()


async def get_benchmark_summary() -> dict:
    async with get_db() as db:
        cur = await db.execute(
            """
            SELECT model,
                   COUNT(*) AS runs,
                   AVG(latency_ms) AS avg_ms,
                   AVG(CASE WHEN rating > 0 THEN rating END) AS avg_rating
            FROM benchmark_results
            WHERE error IS NULL
            GROUP BY model
            """
        )
        model_stats = [dict(row) for row in await cur.fetchall()]

        cur2 = await db.execute(
            """
            SELECT model, category, AVG(latency_ms) AS avg_ms
            FROM benchmark_results
            WHERE error IS NULL
            GROUP BY model, category
            """
        )
        cat_stats = [dict(row) for row in await cur2.fetchall()]

        cur3 = await db.execute(
            """
            SELECT id, question_id, category, question,
                   model, answer, latency_ms, rating, error
            FROM benchmark_results
            ORDER BY id DESC LIMIT 60
            """
        )
        recent = [dict(row) for row in await cur3.fetchall()]

    return {
        "model_stats": model_stats,
        "cat_stats": cat_stats,
        "recent": recent,
    }
