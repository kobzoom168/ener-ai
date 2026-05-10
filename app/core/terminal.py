import asyncio

from fastapi import WebSocket


async def handle_terminal_ws(websocket: WebSocket):
    """Proxy a local bash shell over WebSocket."""
    await websocket.accept()

    process = await asyncio.create_subprocess_shell(
        "bash",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )

    async def read_output():
        while True:
            data = await process.stdout.read(1024)
            if not data:
                break
            await websocket.send_text(data.decode("utf-8", errors="replace"))

    async def read_input():
        while True:
            data = await websocket.receive_text()
            if process.stdin:
                process.stdin.write(data.encode())
                await process.stdin.drain()

    output_task = asyncio.create_task(read_output())
    input_task = asyncio.create_task(read_input())

    done, pending = await asyncio.wait(
        {output_task, input_task},
        return_when=asyncio.FIRST_COMPLETED,
    )

    for task in pending:
        task.cancel()

    try:
        if process.returncode is None:
            process.terminate()
    except Exception:
        pass

    try:
        await process.wait()
    except Exception:
        pass

    for task in done:
        try:
            await task
        except Exception:
            pass
