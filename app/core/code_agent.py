"""Autonomous Code Agent — propose, approve, apply, verify, rollback."""
import asyncio
import difflib
import json
import os
import random
import string
import tempfile
from pathlib import Path

PROJECT_ROOT = Path("/app").resolve()
ALLOWED_WRITE_PATHS = ["app/", "tests/"]
ALLOWED_TOP_FILES = {"Dockerfile", "docker-compose.yml", "requirements.txt"}
DENIED_PARTS = {".git", ".env", "data", "backups", "__pycache__"}

_apply_lock = asyncio.Lock()


def generate_token(n: int = 8) -> str:
    return "ENER-" + "".join(random.choices(string.ascii_uppercase + string.digits, k=n))


def resolve_safe_path(rel_path: str) -> Path:
    rel = Path(rel_path.replace("\\", "/"))
    if rel.is_absolute() or ".." in rel.parts:
        raise ValueError(f"Invalid path: {rel_path}")
    if any(p in DENIED_PARTS for p in rel.parts):
        raise ValueError(f"Denied path: {rel_path}")
    full = (PROJECT_ROOT / rel).resolve()
    allowed = (
        any(str(full).startswith(str((PROJECT_ROOT / p).resolve())) for p in ALLOWED_WRITE_PATHS)
        or rel.as_posix() in ALLOWED_TOP_FILES
    )
    if not allowed:
        raise ValueError(f"Path outside allowed area: {rel_path}")
    return full


def make_diff(path: str, old_content: str, new_content: str) -> str:
    old_lines = old_content.splitlines(keepends=True)
    new_lines = new_content.splitlines(keepends=True)
    diff = difflib.unified_diff(
        old_lines, new_lines, fromfile=f"a/{path}", tofile=f"b/{path}"
    )
    return "".join(diff)


async def run_git(args: list[str], timeout: int = 30) -> dict:
    env = {
        "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        "HOME": "/root",
        "GIT_AUTHOR_NAME": "Ener-AI",
        "GIT_AUTHOR_EMAIL": "ai@ener-ai.local",
        "GIT_COMMITTER_NAME": "Ener-AI",
        "GIT_COMMITTER_EMAIL": "ai@ener-ai.local",
    }
    proc = await asyncio.create_subprocess_exec(
        "git", *args,
        cwd=str(PROJECT_ROOT),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.communicate()
        return {"ok": False, "output": "Git timeout", "returncode": -1}
    out = stdout.decode("utf-8", errors="replace").strip()
    err = stderr.decode("utf-8", errors="replace").strip()
    return {"ok": proc.returncode == 0, "output": out or err, "returncode": proc.returncode}


async def get_current_commit() -> str:
    r = await run_git(["rev-parse", "HEAD"])
    return r["output"][:12] if r["ok"] else "unknown"


async def _run_syntax_check() -> dict:
    env = {
        "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        "HOME": "/root",
        "PYTHONUNBUFFERED": "1",
    }
    proc = await asyncio.create_subprocess_exec(
        "python", "-m", "compileall", "app",
        cwd=str(PROJECT_ROOT),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=60)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.communicate()
        return {"ok": False, "output": "Syntax check timeout"}
    out = stdout.decode("utf-8", errors="replace")
    err = stderr.decode("utf-8", errors="replace")
    return {"ok": proc.returncode == 0, "output": out or err}


async def _atomic_write_file(path: str, new_content: str) -> str:
    """Write file atomically. Returns unified diff."""
    target = resolve_safe_path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    old_content = target.read_text(encoding="utf-8") if target.exists() else ""
    diff = make_diff(path, old_content, new_content)
    fd, tmp = tempfile.mkstemp(dir=str(target.parent), prefix=f".{target.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(new_content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, target)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    return diff


def apply_patch_to_content(content: str, patch: dict) -> str:
    """Apply a single patch operation to file content."""
    operation = patch.get("operation", "append")
    search_text = patch.get("search_text", "")
    new_code = patch.get("new_code", "")

    if operation == "append":
        return content.rstrip() + "\n\n" + new_code + "\n"

    if operation == "insert_after":
        if search_text and search_text in content:
            idx = content.index(search_text) + len(search_text)
            newline_idx = content.find("\n", idx)
            if newline_idx == -1:
                return content + "\n" + new_code
            return content[:newline_idx + 1] + new_code + "\n" + content[newline_idx + 1:]
        # Fallback: append
        return content.rstrip() + "\n\n" + new_code + "\n"

    if operation == "insert_before":
        if search_text and search_text in content:
            idx = content.index(search_text)
            return content[:idx] + new_code + "\n" + content[idx:]
        return new_code + "\n" + content

    if operation == "replace":
        if search_text and search_text in content:
            return content.replace(search_text, new_code, 1)
        raise ValueError(f"search_text not found: {search_text[:80]!r}")

    raise ValueError(f"Unknown operation: {operation}")


async def create_code_change_request(feature_request: str, patches: list[dict]) -> dict:
    """
    Create a pending code change request using patch operations.
    patches: [{file_path, operation, search_text, new_code, description}]
    Returns request dict with approval_token.
    """
    import uuid
    from app.core.database import get_db

    request_id = str(uuid.uuid4())[:8]
    token = generate_token()
    base_commit = await get_current_commit()

    # Apply all patches per-file and build diff
    full_diff = ""
    patch_files: dict[str, str] = {}  # path -> final content after all patches

    for patch in patches:
        path = patch["file_path"]
        target = PROJECT_ROOT / path
        old_content = target.read_text(encoding="utf-8") if target.exists() else ""
        current = patch_files.get(path, old_content)
        new_content = apply_patch_to_content(current, patch)
        patch_files[path] = new_content
        full_diff += make_diff(path, old_content, new_content)

    files_to_write = [{"path": k, "new_content": v} for k, v in patch_files.items()]
    plan_summary = "\n".join(
        f"- {p['file_path']}: [{p['operation']}] {p.get('description', '')}"
        for p in patches
    )
    files_json = json.dumps(files_to_write)

    async with get_db() as db:
        await db.execute(
            """INSERT INTO code_change_requests
               (id, feature_request, status, plan_summary, proposed_diff,
                proposed_files_json, approval_token, base_commit)
               VALUES (?,?,?,?,?,?,?,?)""",
            (request_id, feature_request, "pending_approval",
             plan_summary, full_diff, files_json, token, base_commit),
        )
        await db.commit()

    return {
        "request_id": request_id,
        "token": token,
        "plan_summary": plan_summary,
        "diff_preview": full_diff[:1500],
        "file_count": len(patch_files),
        "base_commit": base_commit,
    }


async def apply_code_change(request_id: str) -> dict:
    """
    Apply approved code change. Acquires lock, writes files,
    runs syntax check, commits. Rolls back on failure.
    """
    from app.core.database import get_db, update_code_request_status

    async with _apply_lock:
        async with get_db() as db:
            cur = await db.execute(
                "SELECT * FROM code_change_requests WHERE id=?", (request_id,)
            )
            req = await cur.fetchone()
        if not req:
            return {"ok": False, "error": "Request not found"}
        req = dict(req)

        if req["status"] != "approved":
            return {"ok": False, "error": f"Wrong status: {req['status']}"}

        await update_code_request_status(
            request_id, "applying",
            attempt_count=req["attempt_count"] + 1,
        )

        base_commit = req["base_commit"]
        files = json.loads(req["proposed_files_json"])
        written: list[str] = []

        try:
            # 1. Create work branch
            branch = f"ai/ener-{request_id}"
            await run_git(["checkout", "-b", branch])

            # 2. Write files atomically
            for f in files:
                await _atomic_write_file(f["path"], f["new_content"])
                written.append(f["path"])

            # 3. Syntax check
            check = await _run_syntax_check()
            if not check["ok"]:
                raise RuntimeError(f"Syntax error:\n{check['output'][:500]}")

            # 4. Git commit
            await run_git(["add", "-A"])
            commit_msg = f"AI: {req['feature_request'][:60]}"
            commit_r = await run_git(["commit", "-m", commit_msg])
            if not commit_r["ok"]:
                raise RuntimeError(f"Git commit failed: {commit_r['output']}")

            # 5. Merge to main
            await run_git(["checkout", "main"])
            merge_r = await run_git(["merge", "--ff-only", branch])
            if not merge_r["ok"]:
                await run_git(["merge", branch, "--no-ff", "-m", f"Merge {branch}"])

            # 6. Push
            await run_git(["push", "origin", "main"])

            await update_code_request_status(request_id, "success", work_branch=branch)

            return {
                "ok": True,
                "files_written": written,
                "branch": branch,
                "message": "Changes applied and pushed to main ✅",
            }

        except Exception as exc:
            await run_git(["reset", "--hard", base_commit])
            await run_git(["checkout", "main"])
            await update_code_request_status(
                request_id, "failed",
                last_error=str(exc)[:500],
            )
            return {"ok": False, "error": str(exc), "rolled_back": True}


async def deploy_after_apply() -> dict:
    """Run docker compose up --build -d after successful apply."""
    env = {
        "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        "HOME": "/root",
    }
    proc = await asyncio.create_subprocess_exec(
        "docker", "compose", "up", "-d", "--build",
        cwd="/root/ener-ai",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=300)
    except asyncio.TimeoutError:
        proc.kill()
        return {"ok": False, "output": "Deploy timeout after 300s"}
    out = stdout.decode("utf-8", errors="replace")
    err = stderr.decode("utf-8", errors="replace")
    return {"ok": proc.returncode == 0, "output": out or err}
