from app.core.department import BaseDepartmentHead


class EnerHead(BaseDepartmentHead):
    name = "EnerHead"
    dept_key = "ener"
    emoji = "⚡"

    routing_rules = [
        (r"ไพ่|tarot|ดวง|พยากรณ์", "tarot"),
        (r"caption|tiktok|facebook|โพสต์|content|script|คอนเทนต์", "content"),
        (r"พระ|ener|scan|พลังงาน|เครื่องราง", "ener"),
    ]
    default_agent = "ener"
