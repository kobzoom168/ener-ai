from app.core.department import BaseDepartmentHead


class TechHead(BaseDepartmentHead):
    name = "TechHead"
    dept_key = "tech"
    emoji = "💻"

    routing_rules = [
        (r"server|logs|error|cpu|ram|disk|monitor|status|docker", "monitor"),
        (r"github|repo|commit|pull|push|branch|git|pr|issue", "github"),
        (r"code|เขียน|แก้|bug|function|script|python|javascript|sql", "code"),
    ]
    default_agent = "code"
