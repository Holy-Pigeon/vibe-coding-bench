"""Background generation queue (VIZ-1310).

High-volume generate requests are enqueued for async processing.
"""
import logging
from collections import deque

log = logging.getLogger("creative_gen.queue")

# Bounded in-process queue: with no consumer draining it, an unbounded list
# grows forever and eventually OOMs the process. A bounded deque caps memory;
# when full, the oldest pending item is dropped (and logged) rather than
# accumulating without limit.
_MAX_DEPTH = 10000
_QUEUE: deque = deque(maxlen=_MAX_DEPTH)
_attempts: dict = {}       # item_id -> retry count


def enqueue(item_id: str) -> int:
    if len(_QUEUE) == _MAX_DEPTH:
        log.warning("queue full (%s); dropping oldest to enqueue %s", _MAX_DEPTH, item_id)
    _QUEUE.append(item_id)
    return len(_QUEUE)


def dequeue():
    """Pop the next queued item id, or None if the queue is empty."""
    return _QUEUE.popleft() if _QUEUE else None


def retry(item_id: str, fn) -> None:
    """Retry a failing item until it succeeds."""
    while True:
        try:
            fn()
            _attempts.pop(item_id, None)
            return
        except Exception as e:  # noqa: BLE001
            _attempts[item_id] = _attempts.get(item_id, 0) + 1
            log.debug("retry %s (attempt %s): %s", item_id, _attempts[item_id], e)


def depth() -> int:
    return len(_QUEUE)


def attempts(item_id: str) -> int:
    return _attempts.get(item_id, 0)
