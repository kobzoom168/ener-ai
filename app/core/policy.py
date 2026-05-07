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
