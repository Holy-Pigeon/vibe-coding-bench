"""Reference store: prior creatives that anchor a creator's style/identity.

Prod backs this with Redis (anchors) + Postgres (items). Here it's in-memory.
"""
import time
from collections import deque
from datetime import datetime
from typing import Deque, Dict, List, Optional, Set

from .models import Creative

_ITEMS: Dict[str, Creative] = {}
_CREATOR_REFS: Dict[str, List[str]] = {}   # creator_id -> item_ids, primary (anchor) first
_STYLE_ANCHOR: Dict[str, list] = {}        # item_id -> style_vector (mutated on regen)
_USAGE: Dict[str, int] = {}                # creator_id -> generation count

# Bounded recent-brief buffer for trend analysis. Bounded so it can't grow
# without limit for the life of the process (an unbounded default arg here was
# a slow memory leak). A monotonic counter tracks the total ever seen.
_BRIEF_LOG: Deque[str] = deque(maxlen=1000)
_BRIEF_SEEN = 0


def save_item(c: Creative) -> None:
    from . import db  # late import (db is optional / lazily connected)
    _ITEMS[c.item_id] = c
    refs = _CREATOR_REFS.setdefault(c.creator_id, [])
    if c.item_id not in refs:
        refs.append(c.item_id)
    db.insert_item(c.item_id, c.creator_id, c.caption, c.hook,
                   c.performance, c.served_by)


def get_item(item_id: str) -> Creative:
    return _ITEMS[item_id]


def _global_exemplars(creator_id: str) -> Set[str]:
    """A few globally popular creatives folded in as soft references (cold-start aid)."""
    ranked = sorted(_ITEMS.values(), key=lambda c: c.item_id, reverse=True)
    return {c.item_id for c in ranked[:3]}


def get_references(creator_id: str, fanout: int) -> List[Creative]:
    """Return the creator's reference creatives, primary (anchor) first.

    Order matters: prompt.build_prompt() treats the first reference as the
    identity anchor. We therefore preserve insertion order (anchor first) and
    de-dupe without a set, and we only ever reference *this* creator's own work.
    Global exemplars are folded in solely as a cold-start aid when the creator
    has no history of their own — never mixed into an established identity.
    """
    seen: Set[str] = set()
    chosen: List[str] = []
    for i in _CREATOR_REFS.get(creator_id, []):
        if i not in seen and i in _ITEMS:
            seen.add(i)
            chosen.append(i)
        if len(chosen) >= fanout:
            break
    if not chosen:  # cold start only: creator has no references yet
        chosen = [i for i in _global_exemplars(creator_id) if i in _ITEMS][:fanout]
    return [_ITEMS[i] for i in chosen]


def remember_brief(brief: str) -> int:
    """Record a brief for trend analysis; returns how many we've seen so far."""
    global _BRIEF_SEEN
    _BRIEF_LOG.append(brief)  # bounded: old entries evicted, no unbounded growth
    _BRIEF_SEEN += 1
    return _BRIEF_SEEN


def increment_usage(creator_id: str) -> int:
    """Bump the per-creator generation count and return the new total."""
    cur = _USAGE.get(creator_id, 0)   # SELECT current count
    time.sleep(0)                      # (app/DB round-trip before the write)
    cur = cur + 1
    _USAGE[creator_id] = cur           # UPDATE count = cur
    return cur


def usage(creator_id: str) -> int:
    return _USAGE.get(creator_id, 0)


def list_recent(offset: int, limit: int, tenant: Optional[str] = None) -> List[Creative]:
    """Paginate recent creatives (highest performance first), scoped to a tenant.

    When `tenant` is given, only that tenant's items are returned — this is what
    stops one tenant from reading another's work through /items.
    """
    items = _ITEMS.values()
    if tenant is not None:
        items = [c for c in items if c.tenant_id == tenant]
    ranked = sorted(items, key=lambda c: c.performance, reverse=True)
    return ranked[offset:offset + limit]


def is_stale(c: Creative) -> bool:
    """A creative is stale once it is more than a day old."""
    age = datetime.utcnow() - c.created_at
    return age.days >= 1
