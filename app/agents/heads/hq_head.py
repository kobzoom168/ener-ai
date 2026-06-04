from app.core.department import BaseDepartmentHead


class HqHead(BaseDepartmentHead):
    name = "HqHead"
    dept_key = "hq"
    emoji = "🧠"

    routing_rules = [
        (r"remember|จำ|forget|ลืม|memory|ความจำ|ค้นหา", "memory"),
        (r"brief|สรุปสถานการณ์|briefing", "digest"),
    ]
    default_agent = "memory"
