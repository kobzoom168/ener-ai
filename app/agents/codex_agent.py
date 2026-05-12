"""Codex CLI agent — calls OpenAI Codex using ChatGPT Plus billing."""
import asyncio
import os


async def run_codex(task: str, directory: str = "/root/ener-ai") -> dict:
    """
    Run Codex CLI non-interactively and return result.
    Uses ChatGPT Plus account (no separate API billing).
    """
    if not os.path.isdir(directory):
        directory = "/root/ener-ai"

    try:
        proc = await asyncio.create_subprocess_exec(
            "codex",
            "--quiet",
            "--approval-policy", "auto-edit",
            task,
            cwd=directory,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            stdin=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(input=b"1\n"),
            timeout=120.0,
        )
        output = stdout.decode("utf-8", errors="replace")
        error = stderr.decode("utf-8", errors="replace")
        return {
            "ok": proc.returncode == 0,
            "output": output or error,
            "returncode": proc.returncode,
        }
    except asyncio.TimeoutError:
        return {"ok": False, "output": "Timeout after 120s", "returncode": -1}
    except FileNotFoundError:
        return {
            "ok": False,
            "output": "Codex CLI not found. Run: npm install -g @openai/codex",
            "returncode": -1,
        }
    except Exception as exc:
        return {"ok": False, "output": str(exc), "returncode": -1}


async def run_codex_on_file(task: str, file_path: str) -> dict:
    """Run Codex on a specific file."""
    full_task = f"{task} — focus on file {file_path}"
    return await run_codex(full_task)
