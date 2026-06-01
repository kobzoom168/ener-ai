"""Server introspection helpers for AI tools (containers, projects, logs, nginx, env)."""
from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any

_PROJECT_ALIASES: dict[str, str] = {
    "ener-ai": "ener-ai",
    "ener_ai": "ener-ai",
    "enerai": "ener-ai",
    "ener-scan": "ener-scan",
    "ener_scan": "ener-scan",
    "enerscan": "ener-scan",
    "ener-scan-pro": "ener-scan-pro",
    "ener_scan_pro": "ener-scan-pro",
    "enerscanpro": "ener-scan-pro",
}

_SERVER_ROOTS = ("/root",)
_NGINX_CONFIGS = (
    "/etc/nginx/sites-enabled/ener-scan",
    "/etc/nginx/sites-enabled/ener-ai",
    "/etc/nginx/sites-enabled/ener-local",
)
_SKIP_DIRS = {"node_modules", ".git", "__pycache__", "venv", ".venv", "dist", "build"}
_KEY_EXTENSIONS = (".py", ".js", ".ts", ".tsx", ".json", ".yml", ".yaml", ".env.example")


def _run(cmd: list[str], *, timeout: int = 20) -> str:
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        output = f"{result.stdout or ''}{result.stderr or ''}".strip()
        if output:
            return output
        return f"(exit code {result.returncode})"
    except FileNotFoundError:
        return f"command not found: {cmd[0]}"
    except subprocess.TimeoutExpired:
        return f"timeout after {timeout}s: {' '.join(cmd[:3])}"
    except Exception as exc:
        return f"error: {exc}"


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent.parent


def project_path(project: str) -> Path:
    key = _PROJECT_ALIASES.get(str(project or "").strip().lower(), str(project or "").strip())
    if not key:
        key = "ener-ai"
    for root in _SERVER_ROOTS:
        candidate = Path(root) / key
        if candidate.exists():
            return candidate
    local = _repo_root()
    if local.name == key or (local / "app").exists():
        return local
    return Path(f"/root/{key}")


def detect_project_from_text(text: str, default: str = "ener-ai") -> str:
    lowered = str(text or "").lower()
    for alias, canonical in _PROJECT_ALIASES.items():
        if alias.replace("_", "-") in lowered or alias.replace("-", "_") in lowered:
            return canonical
    if "scan-pro" in lowered or "scan pro" in lowered:
        return "ener-scan-pro"
    if "scan" in lowered:
        return "ener-scan"
    return default


def _resolve_container_name(service: str) -> str:
    listing = _run(["docker", "ps", "--format", "{{.Names}}"])
    names = [line.strip() for line in listing.splitlines() if line.strip()]
    if service in names:
        return service
    for name in names:
        if service in name:
            return name
    return service


def get_server_overview() -> dict[str, Any]:
    import psutil

    containers = _run(
        ["docker", "ps", "--format", "{{.Names}}\t{{.Status}}\t{{.Ports}}"]
    )
    projects: dict[str, str] = {}
    for proj in ("ener-ai", "ener-scan", "ener-scan-pro"):
        path = project_path(proj)
        if path.exists():
            du_out = _run(["du", "-sh", str(path)])
            projects[str(path)] = (
                du_out.split("\t")[0].strip() if "\t" in du_out else du_out.split()[0]
            )

    ports = _run(["ss", "-tlnp"])
    if ports.startswith("command not found"):
        ports = _run(["netstat", "-tlnp"])

    ram = psutil.virtual_memory()
    disk = psutil.disk_usage("/")
    return {
        "containers": containers,
        "projects": projects,
        "ports_sample": "\n".join(ports.splitlines()[:40]),
        "cpu_percent": round(float(psutil.cpu_percent(interval=0.5)), 1),
        "ram_gb": f"{ram.used / 1e9:.1f}/{ram.total / 1e9:.1f}",
        "disk_gb": f"{disk.used / 1e9:.0f}/{disk.total / 1e9:.0f}",
        "disk_percent": round(float(disk.percent), 1),
    }


def get_project_structure(project: str = "ener-ai") -> dict[str, Any]:
    path = project_path(project)
    if not path.exists():
        return {"error": f"project not found: {project}", "path": str(path)}

    git_log = _run(["git", "-C", str(path), "log", "--oneline", "-5"])
    git_status = _run(["git", "-C", str(path), "status", "--short"]) or "clean"
    branch = _run(["git", "-C", str(path), "branch", "--show-current"])

    key_files: list[str] = []
    try:
        for root, dirs, files in os.walk(path):
            dirs[:] = [d for d in dirs if d not in _SKIP_DIRS and not d.startswith(".")]
            rel_root = root.replace(str(path), "").lstrip(os.sep)
            level = rel_root.count(os.sep) if rel_root else 0
            if level > 2:
                dirs.clear()
                continue
            for filename in files:
                if filename.endswith(_KEY_EXTENSIONS):
                    rel = os.path.join(rel_root, filename).replace("\\", "/")
                    key_files.append(f"/{rel}" if not rel.startswith("/") else rel)
                if len(key_files) >= 50:
                    break
            if len(key_files) >= 50:
                break
    except Exception as exc:
        key_files = [f"walk error: {exc}"]

    return {
        "project": project,
        "path": str(path),
        "branch": branch.strip(),
        "git_log": git_log,
        "git_status": git_status.strip() or "clean",
        "key_files": key_files[:50],
    }


def get_service_logs(service: str = "ener-ai", lines: int = 20) -> dict[str, str]:
    service_name = _resolve_container_name(str(service or "ener-ai").strip())
    try:
        line_count = max(5, min(int(lines), 200))
    except (TypeError, ValueError):
        line_count = 20
    output = _run(["docker", "logs", "--tail", str(line_count), service_name], timeout=30)
    combined = output[-3000:]
    return {"service": service_name, "lines": line_count, "logs": combined}


def get_nginx_config() -> dict[str, str]:
    configs: dict[str, str] = {}
    for config_path in _NGINX_CONFIGS:
        path = Path(config_path)
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
            configs[str(path)] = text[:8000]
        except Exception as exc:
            configs[str(path)] = f"read error: {exc}"
    if not configs:
        return {"note": "no nginx site configs found on this host"}
    return configs


def get_env_summary(project: str = "ener-scan-pro") -> dict[str, str]:
    path = project_path(project) / ".env"
    if not path.is_file():
        return {"error": f".env not found for {project}", "path": str(path)}

    summary: dict[str, str] = {}
    try:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            eq = line.find("=")
            if eq < 1:
                continue
            key = line[:eq].strip()
            val = line[eq + 1 :].strip().strip('"').strip("'")
            sensitive = any(
                token in key.upper()
                for token in ("KEY", "SECRET", "TOKEN", "PASSWORD", "PASS", "PRIVATE")
            )
            summary[key] = "***" if sensitive else val[:80]
    except Exception as exc:
        return {"error": str(exc), "path": str(path)}
    return summary


def format_tool_payload(payload: dict[str, Any] | list[Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _message_needs_overview(text: str) -> bool:
    t = text.lower()
    return any(
        k in t
        for k in (
            "container",
            "docker",
            "server overview",
            "ภาพรวม server",
            "เครื่อง server",
            "port ",
            "ports",
            "ทรัพยากร",
        )
    )


def _message_needs_structure(text: str) -> bool:
    t = text.lower()
    return any(
        k in t
        for k in (
            "cursor prompt",
            "git commit",
            "git status",
            "codebase",
            "file structure",
            "โครงสร้าง",
            "structure",
            "commit ล่าสุด",
            "แก้ bug",
            "แก้โค้ด",
            "ener-scan",
            "ener-ai",
            "ener-scan-pro",
        )
    )


def _message_needs_logs(text: str) -> bool:
    t = text.lower()
    return any(
        k in t
        for k in (
            "log",
            "logs",
            "error",
            "traceback",
            "พัง",
            "crash",
        )
    )


def _message_needs_nginx(text: str) -> bool:
    t = text.lower()
    return any(
        k in t
        for k in (
            "nginx",
            "routing",
            "reverse proxy",
            "proxy",
            "sites-enabled",
            "domain",
            "my-ener",
        )
    )


def _message_needs_env(text: str) -> bool:
    t = text.lower()
    if any(k in t for k in ("password", "secret", "api key")):
        return False
    return any(
        k in t
        for k in (
            ".env",
            "env ",
            "environment",
            "config",
            "settings",
            "ตั้งค่า",
        )
    )


async def build_workspace_server_tools_context(message: str) -> str:
    """Prefetch server tool output for workspace chat (no tool-calling in stream)."""
    from app.core.tools import execute_tool

    sections: list[str] = []
    project = detect_project_from_text(message)

    if _message_needs_structure(message) or "cursor" in message.lower():
        data = await execute_tool("get_project_structure", {"project": project})
        sections.append(
            "=== โครงสร้างโปรเจกต์จริง (get_project_structure) ===\n" + data
        )

    if _message_needs_overview(message):
        data = await execute_tool("get_server_overview", {})
        sections.append("=== ภาพรวม Server (get_server_overview) ===\n" + data)

    if _message_needs_logs(message):
        service = project
        if "ener-scan-pro" in message.lower() or "scan-pro" in message.lower():
            service = "ener-scan-pro"
        elif "ener-scan" in message.lower():
            service = "ener-scan"
        data = await execute_tool(
            "get_service_logs",
            {"service": service, "lines": 30},
        )
        sections.append("=== Service Logs (get_service_logs) ===\n" + data)

    if _message_needs_nginx(message):
        data = await execute_tool("get_nginx_config", {})
        sections.append("=== Nginx Config (get_nginx_config) ===\n" + data)

    if _message_needs_env(message):
        data = await execute_tool("get_env_summary", {"project": project})
        sections.append("=== Env Summary (get_env_summary, secrets redacted) ===\n" + data)

    if not sections:
        return ""

    guide = (
        "กฎ: ใช้ข้อมูลด้านบนที่ดึงจาก server จริง — ห้ามแต่ง path/container ที่ไม่มีในข้อมูล\n"
        "ถ้าช่วยเขียน Cursor prompt ให้อ้าง file paths และ git state จาก get_project_structure"
    )
    return guide + "\n\n" + "\n\n".join(sections)
