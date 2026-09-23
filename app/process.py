"""All child processes are isolated and reaped, including cancellation on disconnect."""
import asyncio
import os
import signal


async def stop_process(proc):
    if proc.returncode is None:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            await asyncio.wait_for(proc.wait(), 3)
        except asyncio.TimeoutError:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            await proc.wait()


async def capture(command, timeout=90):
    proc = await asyncio.create_subprocess_exec(*command, stdout=asyncio.subprocess.PIPE,
                                              stderr=asyncio.subprocess.PIPE, start_new_session=True)
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout)
        if proc.returncode:
            raise RuntimeError(err.decode(errors="replace")[-1800:] or "The process failed")
        return out.decode()
    finally:
        await stop_process(proc)
