"""Daily auto-post: AI picks a สายมู topic -> Thai short (cloned voice + BGM + twist)
-> publish to the Ener Scan Facebook page via Postiz. Run by cron inside the ener-ai
container. Logs one line per run; exits non-zero on failure so cron mail/log shows it.

    docker exec ener-ai-ener-ai-1 python /app/scripts/auto_post_mystery.py
"""
import asyncio
import sys
import traceback
from datetime import datetime, timezone

from app.agents.vdo_agent import make_mystery_short
from app.agents import postiz_client as postiz


def _log(msg: str) -> None:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")
    print(f"[{ts}] {msg}", flush=True)


async def main() -> int:
    _log("auto-post: generating mystery short…")
    try:
        r = await make_mystery_short()  # no topic -> AI picks one
    except Exception as exc:
        _log(f"RENDER EXC: {exc}\n{traceback.format_exc()}")
        return 1
    if not r.get("ok"):
        _log(f"RENDER FAIL: {r.get('error')}")
        return 1

    title = r.get("title", "")
    mp4 = r.get("mp4", "")
    caption = r.get("caption", "") or title
    _log(f"rendered: {title!r} -> {mp4} (bg={r.get('bg_count')}, dur={r.get('duration')}s)")

    try:
        ok, msg = await postiz.post_video(mp4, caption, when="now")
    except Exception as exc:
        _log(f"POST EXC: {exc}")
        return 2
    _log(f"{'POSTED ✅' if ok else 'POST FAIL ❌'}: {msg}")
    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
