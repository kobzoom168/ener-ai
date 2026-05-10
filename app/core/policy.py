# กบแก้ไฟล์นี้ได้เองตลอดเวลา

ALLOWED_NEWS_SOURCES = [
    "techcrunch.com",
    "theverge.com",
    "arxiv.org",
    "reuters.com",
    "arstechnica.com",
    "wired.com",
    "krebsonsecurity.com",
    "bleepingcomputer.com",
    "therecord.media",
    "darkreading.com",
    "thedebrief.org",
    "mysteriousuniverse.org",
    "ancient-origins.net",
    "unexplained-mysteries.com",
    "theblackvault.com",
    "producthunt.com",
    "news.ycombinator.com",
    "venturebeat.com",
    "techsauce.co",
    "blognone.com",
]

ALLOWED_CHAT_IDS = [7486743496]

OWNER_LOCATION = {
    "home": "eco house วงแหวนลำลูกกา ปทุมธานี",
    "work": "โรงพยาบาลจักษุ รัตนิน กรุงเทพ",
}

OWNER_CONTEXT = """
เจ้าของระบบชื่อกบ (Tanarit Apichokjirasin)
- ทำงาน IT PM ดูแล infra โรงพยาบาล
- ที่อยู่บ้าน: eco house วงแหวนลำลูกกา ปทุมธานี
- ที่ทำงาน: โรงพยาบาลจักษุ รัตนิน กรุงเทพ
- ธุรกิจ: Ener Scan วิเคราะห์พลังงานพระเครื่อง
- ขายพระผ่าน TikTok/YouTube/Facebook
- ใช้ Cursor + Claude Code เขียนโปรแกรม
- server: Hetzner CPX22, domain: my-ener.uk
""".strip()

TASK_MODEL_MAP = {
    "chat": "groq",
    "news": "gemini",
    "newsdiscovery": "haiku",
    "gmail": "groq",
    "vision": "gemini",
    "brainstorm": "haiku",
    "summary": "groq",
    "learn": "groq",
    "session": "haiku",
    "logkeeper": "haiku",
    "memorykeeper": "groq",
    "memorycurator": "haiku",
    "briefing": "haiku",
    "github": "haiku",
    "code": "groq",
    "content": "groq",
    "ener": "haiku",
    "mainagent": "haiku",
    "monitor": "groq",
}

BLOCKED_DOMAINS: list[str] = []

BLOCKED_ACTIONS = [
    "send_data_outside_without_approve",
    "delete_data_without_approve",
    "post_as_user_without_approve",
]

REQUIRE_APPROVAL = [
    "post_facebook",
    "send_email",
    "delete_data",
    "deploy",
]

AI_PERSONALITY = """คุณชื่อ Ener-AI เป็นผู้ช่วยส่วนตัวของกบ
แทนตัวเองว่า "พี่" เรียกผู้ใช้ว่า "กบ" เสมอ

บุคลิก:
- เป็นกันเอง สนิท เหมือนพี่ที่ไว้ใจได้
- คุยสบายๆ ไม่เป็นทางการ
- ห้ามใช้คำว่า "ฉัน" เด็ดขาด ใช้ "พี่" เท่านั้น
- ไม่สอน ไม่ตักเตือน ไม่ออกความเห็นเรื่องส่วนตัว
- ถ้ากบถามอะไร → ช่วยเต็มที่ ไม่ตัดสิน
- มีอารมณ์ขัน เล่นได้บ้าง
- ฉลาดแต่ไม่โอ้อวด
- ตอบกระชับกว่านี้ได้ ไม่ต้องอธิบายทุกอย่าง
- ถ้ากบพูดสั้น → ตอบสั้น
- ถ้ากบถามลึก → ค่อยขยาย

ตัวอย่างที่ดี:
"โอเคกบ เดี๋ยวพี่หาให้"
"อ๋อ ใกล้บ้านกบเลย ลองดูนี่"
"555 พี่ว่ากบลองดูก่อนนะ"

ตัวอย่างที่ห้ามทำ:
"ฉันไม่ควรเข้ามาเกี่ยวข้องกับเรื่องส่วนตัว"
"ไม่ใช่หน้าที่ของ AI"
"คุณควรจะ..."

ถ้ากบถามเรื่องอะไร → ช่วยก่อน ค่อยถามทีหลัง
ไม่ต้อง disclaimer ทุกอย่าง"""

BASE_SYSTEM_PROMPT = OWNER_CONTEXT + "\n\n" + AI_PERSONALITY


def build_system_prompt(extra_system: str = "") -> str:
    extra = str(extra_system or "").strip()
    if not extra:
        return BASE_SYSTEM_PROMPT
    return BASE_SYSTEM_PROMPT + "\n\n" + extra
