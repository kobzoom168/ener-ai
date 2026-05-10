import random

from app.core.ai import chat
from app.core.agents import log_agent_run
from app.core.event_log import log_event
from app.core.policy import build_system_prompt

MAJOR_ARCANA = [
    ("The Fool", "ผู้บ้า - จุดเริ่มต้น ความกล้า การผจญภัย"),
    ("The Magician", "นักมายากล - พลังงาน ทักษะ ความตั้งใจ"),
    ("The High Priestess", "นักบวชหญิง - สัญชาตญาณ ความลึกลับ ปัญญาภายใน"),
    ("The Empress", "จักรพรรดินี - ความอุดมสมบูรณ์ ธรรมชาติ ความรัก"),
    ("The Emperor", "จักรพรรดิ - อำนาจ โครงสร้าง ความมั่นคง"),
    ("The Hierophant", "นักบวช - ประเพณี ความเชื่อ การนำทาง"),
    ("The Lovers", "คู่รัก - ความรัก ทางเลือก ความสัมพันธ์"),
    ("The Chariot", "รถศึก - ชัยชนะ การควบคุม ความมุ่งมั่น"),
    ("Strength", "พลัง - ความกล้า ความอดทน พลังภายใน"),
    ("The Hermit", "ฤษี - การค้นหาตัวเอง ความสันโดษ ปัญญา"),
    ("Wheel of Fortune", "วงล้อโชค - โชคชะตา การเปลี่ยนแปลง วัฏจักร"),
    ("Justice", "ความยุติธรรม - ความสมดุล ความจริง กฎแห่งกรรม"),
    ("The Hanged Man", "คนแขวน - การพักผ่อน การยอมรับ มุมมองใหม่"),
    ("Death", "ความตาย - การเปลี่ยนแปลง สิ้นสุด จุดเริ่มต้นใหม่"),
    ("Temperance", "ความพอดี - สมดุล ความอดทน การบูรณาการ"),
    ("The Devil", "ปีศาจ - การยึดติด ความกลัว พันธนาการ"),
    ("The Tower", "หอคอย - การพังทลาย การเปิดเผย การเปลี่ยนแปลงฉับพลัน"),
    ("The Star", "ดาว - ความหวัง การรักษา แรงบันดาลใจ"),
    ("The Moon", "พระจันทร์ - ความลวงตา ความฝัน ความกลัวลึก"),
    ("The Sun", "ดวงอาทิตย์ - ความสำเร็จ ความสุข ความสดใส"),
    ("Judgement", "การพิพากษา - การตื่นรู้ การฟื้นฟู การตัดสินใจ"),
    ("The World", "โลก - ความสมบูรณ์ ความสำเร็จ การเดินทางจบ"),
]

SUITS = ["ไม้เท้า (Wands)", "ถ้วย (Cups)", "ดาบ (Swords)", "เหรียญ (Pentacles)"]
COURT_CARDS = ["เพจ", "อัศวิน", "ราชินี", "ราชา"]
PIP_CARDS = ["เอซ", "2", "3", "4", "5", "6", "7", "8", "9", "10"]

TAROT_SYSTEM = build_system_prompt("""
งานของพี่: ทำนายไพ่ทาโรต์ให้กบ

สไตล์:
- พูดแบบผู้รู้ด้านจิตวิญญาณ ไม่ใช่แค่ AI
- เชื่อมไพ่กับสถานการณ์จริงของกบ
- ตรงไปตรงมา ไม่อ้อมค้อม
- ถ้าไพ่ดี → บอกตรง ถ้าไพ่เตือน → บอกตรงแต่สร้างสรรค์
- ใช้พลังงานและจิตวิญญาณในการอธิบาย
- ท้ายสุด give action ที่ทำได้จริง
""")


def _build_full_deck() -> list[tuple[str, str]]:
    deck = list(MAJOR_ARCANA)
    for suit in SUITS:
        for pip in PIP_CARDS:
            deck.append((f"{pip} of {suit}", f"ไพ่{suit} {pip}"))
        for court in COURT_CARDS:
            deck.append((f"{court} of {suit}", f"{court}แห่ง{suit}"))
    return deck


FULL_DECK = _build_full_deck()


async def _log_tarot_event(
    event_type: str,
    summary: str,
    result: str,
    learned: str | None = None,
) -> None:
    try:
        await log_event(
            agent_name="TarotAgent",
            event_type=event_type,
            summary=summary,
            tags=["tarot", "spiritual"],
            result=result,
            learned=learned,
        )
    except Exception:
        pass


async def draw_cards(n: int = 1) -> list[dict]:
    drawn = random.sample(FULL_DECK, min(n, len(FULL_DECK)))
    cards = []
    for name, thai_name in drawn:
        is_reversed = random.random() < 0.3
        cards.append(
            {
                "name": name,
                "thai_name": thai_name,
                "reversed": is_reversed,
                "position": "กลับหัว 🔄" if is_reversed else "ตั้งตรง ⬆️",
            }
        )
    return cards


@log_agent_run("TarotAgent")
async def read_cards(question: str = "", spread: str = "single") -> str:
    spread_map = {
        "single": 1,
        "three": 3,
        "celtic": 5,
    }
    n = spread_map.get(spread, 1)
    cards = await draw_cards(n)

    spread_labels = {
        1: [""],
        3: ["อดีต/รากเหง้า", "ปัจจุบัน", "อนาคต/แนวโน้ม"],
        5: ["สถานการณ์", "อุปสรรค", "รากเหง้า", "อนาคต", "ผลลัพธ์"],
    }
    labels = spread_labels.get(n, [""] * n)

    cards_text = "\n".join(
        [
            f"{labels[i] + ': ' if labels[i] else ''}**{card['name']}** ({card['position']})"
            for i, card in enumerate(cards)
        ]
    )
    prompt = f"""
ไพ่ที่จั่วได้:
{cards_text}

{"คำถาม: " + question if question else "ดูภาพรวมวันนี้"}

ตีความไพ่เหล่านี้ให้กบ ใช้สไตล์ผู้รู้ด้านจิตวิญญาณ
"""

    try:
        interpretation = await chat(
            prompt,
            system=TAROT_SYSTEM,
            agent="tarot",
        )
        card_display = "\n".join(
            [
                f"🃏 {labels[i] + ': ' if labels[i] else ''}{card['name']} {card['position']}"
                for i, card in enumerate(cards)
            ]
        )
        result = f"{card_display}\n\n{interpretation}"
        await _log_tarot_event(
            "task_done",
            f"read tarot {spread}",
            "success",
            learned=f"cards={n}",
        )
        return result
    except Exception as exc:
        await _log_tarot_event(
            "task_failed",
            f"read tarot failed: {spread}",
            "failure",
            learned=str(exc)[:200],
        )
        return f"พี่จั่วไพ่ให้ไม่ได้ตอนนี้ครับ: {exc}"
