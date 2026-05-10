import asyncio
import os
import pty

from fastapi import WebSocket


async def handle_terminal_ws(websocket: WebSocket):
    """Proxy a local bash shell over WebSocket."""
    await websocket.accept()

    master_fd, slave_fd = pty.openpty()
    process = await asyncio.create_subprocess_shell(
        "bash",
        stdin=slave_fd,
        stdout=slave_fd,
        stderr=slave_fd,
    )
    os.close(slave_fd)

    async def read_output():
        loop = asyncio.get_event_loop()
        while True:
            try:
                data = await loop.run_in_executor(None, os.read, master_fd, 1024)
                if not data:
                    break
                await websocket.send_text(data.decode("utf-8", errors="replace"))
            except Exception:
                break

    async def read_input():
        while True:
            try:
                data = await websocket.receive_text()
                os.write(master_fd, data.encode())
            except Exception:
                break

    try:
        await asyncio.gather(read_output(), read_input())
    finally:
        try:
            os.close(master_fd)
        except Exception:
            pass
        try:
            process.terminate()
        except Exception:
            pass
