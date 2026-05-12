"""Ener Platform — manages multiple projects via Docker Compose."""
import asyncio
import os
import re
import uuid
from pathlib import Path

PLATFORM_ROOT = Path("/root/ener-platform/projects")
COMPOSE_TEMPLATE = """\
services:
  app:
    build: ./repo
    restart: unless-stopped
    env_file:
      - .env
    networks:
      - ener_public
    labels:
      - "ener.platform.managed=true"
      - "ener.platform.project={slug}"
      - "traefik.enable=true"
      - "traefik.http.routers.{slug}.rule=Host(`{domain}`)"
      - "traefik.http.services.{slug}.loadbalancer.server.port={port}"
    deploy:
      resources:
        limits:
          memory: {memory_limit}

networks:
  ener_public:
    external: true
    name: ener_public
"""


async def _run_compose(project_slug: str, *args, timeout: int = 60) -> dict:
    """Run docker compose command for a project."""
    compose_file = PLATFORM_ROOT / project_slug / "docker-compose.yml"
    if not compose_file.exists():
        return {"ok": False, "output": f"compose file not found: {compose_file}"}
    cmd = [
        "docker", "compose",
        "-p", f"ener_{project_slug}",
        "-f", str(compose_file),
    ] + list(args)
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(PLATFORM_ROOT / project_slug),
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        out = stdout.decode("utf-8", errors="replace")
        err = stderr.decode("utf-8", errors="replace")
        return {"ok": proc.returncode == 0, "output": out or err}
    except asyncio.TimeoutError:
        return {"ok": False, "output": f"Timeout after {timeout}s"}
    except Exception as exc:
        return {"ok": False, "output": str(exc)}


async def _find_free_port(start: int = 3001) -> int:
    from app.core.database import get_db

    async with get_db() as db:
        cur = await db.execute("SELECT port FROM platform_projects WHERE port IS NOT NULL")
        used_ports = {r[0] for r in await cur.fetchall()}
    port = start
    while port in used_ports:
        port += 1
    return port


async def create_project(
    name: str,
    project_type: str = "nodejs",
    port: int = None,
    domain: str = None,
    memory_limit: str = "768m",
) -> dict:
    """Create a new managed project."""
    from app.core.database import get_db

    slug = re.sub(r"[^a-z0-9-]", "-", name.lower()).strip("-")
    project_id = str(uuid.uuid4())[:8]
    if not port:
        port = await _find_free_port()
    if not domain:
        domain = f"{slug}.my-ener.uk"

    project_dir = PLATFORM_ROOT / slug
    project_dir.mkdir(parents=True, exist_ok=True)
    (project_dir / "repo").mkdir(exist_ok=True)
    (project_dir / "data").mkdir(exist_ok=True)
    (project_dir / "logs").mkdir(exist_ok=True)

    compose_content = COMPOSE_TEMPLATE.format(
        slug=slug, domain=domain, port=port, memory_limit=memory_limit
    )
    (project_dir / "docker-compose.yml").write_text(compose_content)

    env_file = project_dir / ".env"
    if not env_file.exists():
        env_file.write_text(f"# {name} environment variables\nNODE_ENV=production\n")

    async with get_db() as db:
        await db.execute(
            """INSERT INTO platform_projects
               (id, name, slug, display_name, type, status, port, domain,
                repo_path, compose_path, memory_limit)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (
                project_id, name, slug, name, project_type, "stopped",
                port, domain,
                str(project_dir / "repo"),
                str(project_dir / "docker-compose.yml"),
                memory_limit,
            ),
        )
        await db.commit()

    return {
        "ok": True,
        "project_id": project_id,
        "slug": slug,
        "domain": domain,
        "port": port,
        "path": str(project_dir),
    }


async def deploy_project(slug: str) -> dict:
    """Build and start a project."""
    from app.core.database import get_db

    async with get_db() as db:
        await db.execute(
            "UPDATE platform_projects SET status='deploying' WHERE slug=?", (slug,)
        )
        await db.commit()

    result = await _run_compose(slug, "up", "-d", "--build", timeout=300)

    status = "running" if result["ok"] else "failed"
    async with get_db() as db:
        await db.execute(
            "UPDATE platform_projects SET status=?, last_deploy=datetime('now') WHERE slug=?",
            (status, slug),
        )
        await db.commit()

    return result


async def stop_project(slug: str) -> dict:
    from app.core.database import get_db

    result = await _run_compose(slug, "down")
    async with get_db() as db:
        await db.execute(
            "UPDATE platform_projects SET status='stopped' WHERE slug=?", (slug,)
        )
        await db.commit()
    return result


async def restart_project(slug: str) -> dict:
    return await _run_compose(slug, "restart")


async def get_project_logs(slug: str, lines: int = 50) -> str:
    result = await _run_compose(slug, "logs", f"--tail={lines}", "--no-color")
    return result.get("output", "no logs")


async def get_project_metrics(slug: str) -> dict:
    """Get real-time resource usage via docker stats."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "docker", "stats", f"ener_{slug}-app-1",
            "--no-stream", "--format",
            "{{.CPUPerc}}\t{{.MemUsage}}\t{{.MemPerc}}",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
        line = stdout.decode().strip()
        if line:
            parts = line.split("\t")
            return {
                "cpu": parts[0] if len(parts) > 0 else "0%",
                "memory": parts[1] if len(parts) > 1 else "0/0",
                "mem_percent": parts[2] if len(parts) > 2 else "0%",
            }
    except Exception:
        pass
    return {"cpu": "N/A", "memory": "N/A", "mem_percent": "N/A"}


async def get_all_projects() -> list:
    from app.core.database import get_db

    async with get_db() as db:
        cur = await db.execute(
            """SELECT id, name, slug, type, status, port, domain,
                      last_deploy, memory_limit
               FROM platform_projects ORDER BY created_at DESC"""
        )
        rows = await cur.fetchall()
    return [dict(r) for r in rows]


async def get_server_metrics() -> dict:
    """Get host server resource usage via psutil."""
    try:
        import psutil

        cpu = psutil.cpu_percent(interval=0.5)
        mem = psutil.virtual_memory()
        disk = psutil.disk_usage("/")
        return {
            "cpu_percent": cpu,
            "ram_used_mb": int(mem.used / 1024 / 1024),
            "ram_total_mb": int(mem.total / 1024 / 1024),
            "ram_percent": mem.percent,
            "disk_used_gb": int(disk.used / 1024 / 1024 / 1024),
            "disk_total_gb": int(disk.total / 1024 / 1024 / 1024),
            "disk_percent": disk.percent,
        }
    except ImportError:
        return {
            "cpu_percent": 0,
            "ram_used_mb": 0,
            "ram_total_mb": 0,
            "ram_percent": 0,
            "disk_used_gb": 0,
            "disk_total_gb": 0,
            "disk_percent": 0,
            "error": "psutil not installed",
        }
