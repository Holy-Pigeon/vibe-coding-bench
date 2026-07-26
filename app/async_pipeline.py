"""Async post-generation pipeline — runs after each generate (warm trending, notify)."""
import asyncio
import logging
import time
from concurrent.futures import ThreadPoolExecutor

log = logging.getLogger("creative_gen.async_pipeline")

_trending: list = []

# Bounded pool so post-processing actually runs off the request path. The worker
# is synchronous; previously it *called* the coroutine without awaiting it, so
# `post_generate` never executed (silent no-op + RuntimeWarning) and trending
# warm-up / notifications never happened. We now hand each run to this pool.
_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="post-gen")


async def _warm_trending(item_id: str) -> None:
    await asyncio.sleep(0)
    _trending.append(item_id)


def _notify(item_id: str) -> None:
    # post to the notifications service (sync client) — ~50ms round trip
    time.sleep(0.05)


async def post_generate(item_id: str) -> None:
    await _warm_trending(item_id)
    _notify(item_id)


def _run_post_generate(item_id: str) -> None:
    try:
        asyncio.run(post_generate(item_id))
    except Exception:  # noqa: BLE001 - background work must not crash silently
        log.exception("post_generate failed for %s", item_id)


def schedule_post_generate(item_id: str) -> None:
    """Run the post-generation pipeline in the background (never blocks the request)."""
    _executor.submit(_run_post_generate, item_id)
