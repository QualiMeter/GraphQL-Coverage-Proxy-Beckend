import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context

from anyio.lowlevel import RunVar

_slots = RunVar("coverage_schema_work_slots")
MAX_SCHEMA_WORKERS = 2
_comparison_slots = RunVar("coverage_comparison_work_slots")
MAX_COMPARISON_WORKERS = 2
# Comparisons cannot consume capture capacity or queue ahead of its analyzer
# in asyncio's default executor. Threads are created lazily, at most two per
# process; the executor's normal interpreter shutdown joins outstanding work.
_comparison_executor = ThreadPoolExecutor(
    max_workers=MAX_COMPARISON_WORKERS, thread_name_prefix="coverage-comparison",
)


async def schema_work(fn, *args):
    return await _bounded_work(fn, args, _slots, MAX_SCHEMA_WORKERS)


async def comparison_work(fn, *args):
    return await _bounded_work(fn, args, _comparison_slots, MAX_COMPARISON_WORKERS,
                               executor=_comparison_executor)


async def _bounded_work(fn, args, slots_var, limit, *, executor=None):
    try:
        slots = slots_var.get()
    except LookupError:
        slots = asyncio.Semaphore(limit)
        slots_var.set(slots)
    await slots.acquire()
    # Shield keeps the slot occupied if the request is cancelled: Python cannot
    # stop a running thread. Its result is discarded and no caller writes follow.
    try:
        if executor is None:
            task = asyncio.create_task(asyncio.to_thread(fn, *args))
        else:
            context = copy_context()
            task = asyncio.get_running_loop().run_in_executor(executor, context.run, fn, *args)
    except BaseException:
        slots.release()
        raise

    def finished(task):
        slots.release()
        if not task.cancelled():
            task.exception()  # Retrieve errors even when the caller went away.

    task.add_done_callback(finished)
    return await asyncio.shield(task)
