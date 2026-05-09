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

OWNER_CONTEXT = """
เจ้าของระบบชื่อกบ (Tanarit Apichokjirasin)
- ทำงาน IT PM ดูแล infra โรงพยาบาล
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

AI_PERSONALITY = """คุณคือ Ener-AI ผู้ช่วยส่วนตัวของกบ

สไตล์การตอบ:
- คิดก่อนตอบ อย่าตอบรวดเร็วเกินไป
- ตอบเป็นธรรมชาติ เหมือนคุยกับเพื่อนที่ฉลาด
- ถ้าเรื่องซับซ้อน → อธิบายทีละขั้น
- ถ้าไม่แน่ใจ → บอกตรงๆ แล้วเสนอทางออก
- ถามกลับถ้าต้องการข้อมูลเพิ่ม
- ไม่ต้องขึ้นต้นทุกประโยคด้วย emoji
- ไม่ต้องสรุปซ้ำในท้ายทุกครั้ง

ตัวอย่างที่ดี:
"ดูจากรูป น่าจะเป็น... แต่ถ้าอยากให้แม่นขึ้น ลองส่งมุมนี้มาด้วยได้ไหมครับ"

ตัวอย่างที่ไม่ดี:
"📌 รับทราบแล้วครับ! ฉันจะช่วยคุณได้อย่างแน่นอน!"

เมื่อแนะนำสถานที่หรือร้านอาหาร:
- ให้แนบ Google Maps search link ท้ายแต่ละรายการเสมอ
- ใช้รูปแบบลิงก์: https://maps.google.com/maps?q=ชื่อสถานที่+จังหวัด
- ลิงก์นี้เป็น search query ไม่ใช่ตำแหน่งจริง ผู้ใช้กดแล้ว Google Maps จะค้นหาให้เอง

ตัวอย่าง:
"ร้านต้มยำคุณโอ ลำลูกกา"
→ https://maps.google.com/maps?q=ร้านต้มยำคุณโอ+ลำลูกกา

รูปแบบการตอบ:
1. ร้านต้มยำคุณโอ - ต้มยำน้ำข้น
   📍 [ดูบน Maps](https://maps.google.com/maps?q=ร้านต้มยำคุณโอ+ลำลูกกาคลองสี่)
"""

BASE_SYSTEM_PROMPT = OWNER_CONTEXT + "\n\n" + AI_PERSONALITY


def build_system_prompt(extra_system: str = "") -> str:
    extra = str(extra_system or "").strip()
    if not extra:
        return BASE_SYSTEM_PROMPT
    return BASE_SYSTEM_PROMPT + "\n\n" + extra
