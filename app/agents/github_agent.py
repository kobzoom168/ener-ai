import asyncio

from app.core.ai import chat
from app.core.agents import log_agent_run
from app.core.event_log import log_event
from app.core.policy import build_system_prompt

GITHUB_SYSTEM = build_system_prompt("""
งานของพี่: ช่วยกบจัดการ GitHub
อ่าน code วิเคราะห์ PR issues commits
""")


async def get_github_service():
    from github import Github
    from app.core.config import settings

    token = str(settings.github_token or "").strip()
    if not token:
        return None
    return Github(token)


async def _log_github_event(
    event_type: str,
    summary: str,
    result: str,
    learned: str | None = None,
) -> None:
    try:
        await log_event(
            agent_name="GithubAgent",
            event_type=event_type,
            summary=summary,
            tags=["github", "repo"],
            result=result,
            learned=learned,
        )
    except Exception:
        pass


async def _get_user():
    service = await get_github_service()
    if not service:
        return None, "ยังไม่ได้ตั้งค่า GITHUB_TOKEN ครับกบ"
    try:
        user = await asyncio.to_thread(service.get_user)
        return user, ""
    except Exception as exc:
        return None, f"เชื่อม GitHub ไม่สำเร็จ: {exc}"


@log_agent_run("GithubAgent")
async def list_repos() -> str:
    user, error = await _get_user()
    if error:
        await _log_github_event("warning", "github repos unavailable", "failure", learned=error[:200])
        return error

    try:
        repos = await asyncio.to_thread(lambda: list(user.get_repos())[:10])
    except Exception as exc:
        await _log_github_event("task_failed", "list repos fail", "failure", learned=str(exc)[:200])
        return f"อ่าน repo ไม่สำเร็จ: {exc}"
    if not repos:
        return "ยังไม่พบ repo ในบัญชี GitHub ครับกบ"

    lines = ["📦 Repos ของกบ:"]
    for repo in repos:
        lines.append(f"· {repo.name}")
    await _log_github_event("task_done", "list repos", "success", learned=f"count={len(repos)}")
    return "\n".join(lines)


@log_agent_run("GithubAgent")
async def list_prs(repo_name: str | None = None) -> str:
    user, error = await _get_user()
    if error:
        await _log_github_event("warning", "github prs unavailable", "failure", learned=error[:200])
        return error

    try:
        if repo_name:
            repos = [await asyncio.to_thread(user.get_repo, repo_name)]
        else:
            repos = await asyncio.to_thread(lambda: list(user.get_repos())[:5])
    except Exception as exc:
        await _log_github_event("task_failed", "list prs fail", "failure", learned=str(exc)[:200])
        return f"อ่าน PRs ไม่สำเร็จ: {exc}"

    lines = ["📋 PRs ที่เปิดอยู่:"]
    for repo in repos:
        prs = await asyncio.to_thread(lambda repo=repo: list(repo.get_pulls(state="open"))[:3])
        if prs:
            lines.append(f"\n**{repo.name}**")
            for pr in prs:
                lines.append(f"  · #{pr.number} {pr.title}")

    result = "\n".join(lines) if len(lines) > 1 else "ไม่มี PR เปิดอยู่ครับกบ"
    await _log_github_event("task_done", "list prs", "success", learned=f"repo={repo_name or 'all'}")
    return result


@log_agent_run("GithubAgent")
async def list_issues(repo_name: str | None = None) -> str:
    user, error = await _get_user()
    if error:
        await _log_github_event("warning", "github issues unavailable", "failure", learned=error[:200])
        return error

    try:
        if repo_name:
            repos = [await asyncio.to_thread(user.get_repo, repo_name)]
        else:
            repos = await asyncio.to_thread(lambda: list(user.get_repos())[:3])
    except Exception as exc:
        await _log_github_event("task_failed", "list issues fail", "failure", learned=str(exc)[:200])
        return f"อ่าน issues ไม่สำเร็จ: {exc}"

    lines = ["🐛 Issues ที่เปิดอยู่:"]
    for repo in repos:
        issues = await asyncio.to_thread(lambda repo=repo: list(repo.get_issues(state="open"))[:3])
        issues = [issue for issue in issues if not getattr(issue, "pull_request", None)]
        if issues:
            lines.append(f"\n**{repo.name}**")
            for issue in issues:
                lines.append(f"  · #{issue.number} {issue.title}")

    result = "\n".join(lines) if len(lines) > 1 else "ไม่มี issue เปิดอยู่ครับกบ"
    await _log_github_event("task_done", "list issues", "success", learned=f"repo={repo_name or 'all'}")
    return result


@log_agent_run("GithubAgent")
async def read_file(repo_name: str, file_path: str) -> str:
    user, error = await _get_user()
    if error:
        await _log_github_event("warning", "github read unavailable", "failure", learned=error[:200])
        return error

    try:
        repo = await asyncio.to_thread(user.get_repo, repo_name)
    except Exception as exc:
        await _log_github_event("task_failed", "read github file fail", "failure", learned=str(exc)[:200])
        return f"อ่านไฟล์ไม่สำเร็จ: {exc}"

    paths_to_try = [
        file_path,
        f"app/{file_path}",
        f"src/{file_path}",
    ]

    content = None
    used_path = file_path
    for path in paths_to_try:
        try:
            content = await asyncio.to_thread(repo.get_contents, path)
            used_path = path
            break
        except Exception:
            continue

    if not content:
        try:
            root = await asyncio.to_thread(repo.get_contents, "")
            files = [item.path for item in root if item.type == "file"]
            dirs = [item.path for item in root if item.type == "dir"]
            result = (
                f"ไม่พบ {file_path} กบ\n\n"
                f"โครงสร้าง repo:\n"
                f"📁 {chr(10).join(dirs)}\n"
                f"📄 {chr(10).join(files)}"
            )
            await _log_github_event(
                "warning",
                "read github file missing",
                "failure",
                learned=f"{repo_name}:{file_path}",
            )
            return result
        except Exception as exc:
            await _log_github_event(
                "task_failed",
                "read github file structure fail",
                "failure",
                learned=str(exc)[:200],
            )
            return f"อ่านไม่ได้: {exc}"

    try:
        code = content.decoded_content.decode("utf-8")
    except Exception as exc:
        await _log_github_event(
            "task_failed",
            "decode github file fail",
            "failure",
            learned=str(exc)[:200],
        )
        return f"อ่านไฟล์ไม่สำเร็จ: {exc}"

    analysis = await chat(
        f"ไฟล์ {used_path}:\n{code[:3000]}",
        system=GITHUB_SYSTEM + "\nสรุป code นี้และบอกจุดที่ควรปรับปรุง",
        agent="github",
        preferred_model="haiku",
    )
    await _log_github_event("task_done", "read github file", "success", learned=f"{repo_name}:{used_path}")
    return f"📄 {used_path}\n\n{analysis}"
