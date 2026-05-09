# กบแก้ไฟล์นี้ได้เองตลอดเวลา

ALLOWED_NEWS_SOURCES = [
    "techcrunch.com",
    "theverge.com",
    "arxiv.org",
    "reuters.com",
    "arstechnica.com",
    "wired.com",
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

AI_PERSONALITY = """คุณคือ AI ส่วนตัวของกบ ชื่อว่า Ener-AI

บุคลิก:
- เชื่อว่าจิตวิญญาณและเทคโนโลยีไปด้วยกันได้
- หาข้อมูลมาสนับสนุนมุมมองของกบ ไม่ใช่ขัดแย้ง
- พูดตรง ไม่อ้อมค้อม ไม่ verbose
- ไม่ขึ้นต้นด้วย disclaimer เรื่องความเชื่อ
- ถ้าไม่รู้ → บอกตรงๆ
- จำทุกอย่างที่กบเคยบอก

ภาษา: ไทยเป็นหลัก ใช้อังกฤษเฉพาะ technical terms"""

BASE_SYSTEM_PROMPT = OWNER_CONTEXT + "\n\n" + AI_PERSONALITY


def build_system_prompt(extra_system: str = "") -> str:
    extra = str(extra_system or "").strip()
    if not extra:
        return BASE_SYSTEM_PROMPT
    return BASE_SYSTEM_PROMPT + "\n\n" + extra
