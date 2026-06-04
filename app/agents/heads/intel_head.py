from app.core.department import BaseDepartmentHead


class IntelHead(BaseDepartmentHead):
    name = "IntelHead"
    dept_key = "intel"
    emoji = "📡"

    routing_rules = [
        (r"ข่าว|news|เทคโนโลยี|AI|security|tech", "news"),
        (r"brainstorm|คิด|วิเคราะห์|เปรียบเทียบ|think|idea", "think"),
        (r"สรุป|digest|week|วัน|สัปดาห์|today", "digest"),
    ]
    default_agent = "news"
