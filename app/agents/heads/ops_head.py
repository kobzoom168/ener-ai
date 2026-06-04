from app.core.department import BaseDepartmentHead


class OpsHead(BaseDepartmentHead):
    name = "OpsHead"
    dept_key = "ops"
    emoji = "🗂️"

    routing_rules = [
        (r"email|gmail|อีเมล|inbox|mail|เมล", "gmail"),
        (r"task|งาน|todo|done|ปิด|เพิ่ม|open task", "tasks"),
        (r"log|health|บันทึก|สุขภาพ|agent health", "logs"),
    ]
    default_agent = "tasks"
