"""Autonomous shell execution with whitelist safety for server introspection."""
from __future__ import annotations

import subprocess
from typing import Any

ALLOWED_PREFIXES: tuple[str, ...] = (
    "docker",
    "git",
    "ls",
    "cat",
    "grep",
    "find",
    "df",
    "du",
    "ps",
    "top",
    "free",
    "netstat",
    "ss",
    "curl",
    "ping",
    "systemctl status",
    "nginx -t",
    "python",
    "node --version",
    "npm",
    "pip",
    "journalctl",
    "tail",
    "head",
    "wc",
)

BLOCKED_KEYWORDS: tuple[str, ...] = (
    "rm -rf",
    "mkfs",
    "dd if",
    "shutdown",
    "> /dev",
    "chmod 777 /",
    "reboot",
    "poweroff",
    "init 0",
    "init 6",
    ":(){",
    "wget ",
    "curl |",
    "| bash",
    "| sh",
)

_MAX_OUTPUT = 3000
_DEFAULT_TIMEOUT = 15


def run_shell_command(command: str, timeout: int = _DEFAULT_TIMEOUT) -> dict[str, Any]:
    """Run a whitelisted shell command on the host (via subprocess)."""
    cmd = str(command or "").strip()
    if not cmd:
        return {"error": "empty command", "exit_code": -1}

    cmd_lower = cmd.lower()
    for blocked in BLOCKED_KEYWORDS:
        if blocked in cmd_lower:
            return {"error": f"blocked: {blocked}", "exit_code": -1, "command": cmd}

    allowed = any(cmd.startswith(prefix) for prefix in ALLOWED_PREFIXES)
    if not allowed:
        return {
            "error": "command not in whitelist",
            "allowed_prefixes": list(ALLOWED_PREFIXES),
            "exit_code": -1,
            "command": cmd,
        }

    try:
        timeout_sec = max(3, min(int(timeout), 60))
    except (TypeError, ValueError):
        timeout_sec = _DEFAULT_TIMEOUT

    try:
        result = subprocess.run(
            cmd,
            shell=True,
            capture_output=True,
            text=True,
            timeout=timeout_sec,
        )
        output = f"{result.stdout or ''}{result.stderr or ''}".strip()
        truncated = len(output) > _MAX_OUTPUT
        return {
            "stdout": output[:_MAX_OUTPUT],
            "exit_code": int(result.returncode),
            "truncated": truncated,
            "command": cmd,
        }
    except subprocess.TimeoutExpired:
        return {
            "error": f"timeout after {timeout_sec}s",
            "exit_code": -1,
            "command": cmd,
        }
    except Exception as exc:
        return {"error": str(exc), "exit_code": -1, "command": cmd}


def format_shell_result(payload: dict[str, Any]) -> str:
    import json

    return json.dumps(payload, ensure_ascii=False, indent=2)
