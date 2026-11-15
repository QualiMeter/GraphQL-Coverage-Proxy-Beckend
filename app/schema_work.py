import asyncio

from anyio.lowlevel import RunVar

_slots = RunVar("coverage_schema_work_slots")
MAX_SCHEMA_WORKERS = 2


async def schema_work(fn, *args):
    try:
        slots = _slots.get()
    except LookupError:
        slots = asyncio.Semaphore(MAX_SCHEMA_WORKERS)
        _slots.set(slots)
    await slots.acquire()
    # Shield keeps the slot occupied if the request is cancelled: Python cannot
    # stop a running thread. Its result is discarded and no caller writes follow.
    try:
        task = asyncio.create_task(asyncio.to_thread(fn, *args))
    except BaseException:
        slots.release()
        raise

    def finished(task):
        slots.release()
        if not task.cancelled():
            task.exception()  # Retrieve errors even when the caller went away.

    task.add_done_callback(finished)
    return await asyncio.shield(task)
