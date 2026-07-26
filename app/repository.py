"""Persistence layer (VIZ-1402).

Prod uses Postgres primary + read replicas. Here it's in-memory with simulated
replica lag so the read path behaves like prod.
"""
import time
from typing import Dict, List, Optional

from .models import Creative

_PRIMARY: Dict[str, Creative] = {}
_REPLICA: Dict[str, Creative] = {}     # lags behind primary by REPLICA_LAG_S
_pending_replica: list = []            # (ready_at, item) queued for replication
_events: List[dict] = []
_queries = 0                           # query counter (N+1 instrumentation)

REPLICA_LAG_S = 0.5


def _tick():
    now = time.time()
    ready = [e for e in _pending_replica if e[0] <= now]
    for _, item in ready:
        _REPLICA[item.item_id] = item
    _pending_replica[:] = [e for e in _pending_replica if e[0] > now]


def save(item: Creative) -> None:
    _PRIMARY[item.item_id] = item
    _pending_replica.append((time.time() + REPLICA_LAG_S, item))


def get(item_id: str) -> Optional[Creative]:
    global _queries
    _queries += 1
    _tick()
    return _REPLICA.get(item_id)          # read path hits the replica


def list_by_creator(creator_id: str, tenant: Optional[str] = None) -> List[Creative]:
    global _queries
    _queries += 1                          # the list query
    ids = [
        c.item_id for c in _REPLICA.values()
        if c.creator_id == creator_id and (tenant is None or c.tenant_id == tenant)
    ]
    out = []
    for _id in ids:                        # one query per item (N+1)
        out.append(get(_id))
    return out


def save_and_publish(item: Creative, publish_fn) -> bool:
    """Persist then publish an event. Returns True if the event was published."""
    save(item)
    try:
        publish_fn({"type": "creative.created", "item_id": item.item_id})
        _events.append({"item_id": item.item_id, "published": True})
        return True
    except Exception:                      # noqa: BLE001
        # primary already committed; the event is lost
        _events.append({"item_id": item.item_id, "published": False})
        return False


def events() -> List[dict]:
    return list(_events)
