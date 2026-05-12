"""Codex CLI agent — calls OpenAI Codex using ChatGPT Plus billing."""
import asyncio
import os
import shutil


async def run_codex(task: str, directory: str = "/app") -> dict:
    """Run Codex CLI non-interactively. Uses ChatGPT Plus billing."""
    if not os.path.isdir(directory):
        directory = "/app"

    # Find codex binary
    codex_bin = shutil.which("codex") or "/usr/local/bin/codex"
    if not codex_bin or not os.path.exists(codex_bin):
        return {
            "ok": False,
            "output": f"codex binary not found. which={shutil.which('codex')}",
            "returncode": -1,
        }

    try:
        env = os.environ.copy()
        env["HOME"] = "/root"
        env["PATH"] = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

        proc = await asyncio.create_subprocess_exec(
            codex_bin, "exec",
            "--dangerously-bypass-approvals-and-sandbox",
            task,
            cwd=directory,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            stdin=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(input=b"1\n"),
            timeout=300.0,
        )
        out = stdout.decode("utf-8", errors="replace")
        err = stderr.decode("utf-8", errors="replace")
        combined = out or err or "No output"
        lines = [l for l in combined.splitlines() if l.strip()]
        clean = "\n".join(lines)
        return {
            "ok": proc.returncode == 0,
            "output": clean,
            "returncode": proc.returncode,
        }
    except asyncio.TimeoutError:
        return {"ok": False, "output": "Timeout after 120s", "returncode": -1}
    except Exception as exc:
        return {"ok": False, "output": f"Error: {type(exc).__name__}: {exc}", "returncode": -1}


async def run_codex_on_file(task: str, file_path: str) -> dict:
    """Run Codex focused on a specific file."""
    full_task = f"{task} — focus on file {file_path}"
    return await run_codex(full_task)
