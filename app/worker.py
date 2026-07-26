"""Generation worker.

`regenerate` keeps a per-item *style anchor* and blends each new take into it so
that successive regenerations stay close to the creator's identity.
"""
import logging
from datetime import datetime

from . import async_pipeline, prompt as prompt_mod
from . import billing, cache, context, providers, queue, refimages, repository, store, templates
from .config import REFERENCE_FANOUT, STYLE_BLEND
from .models import Creative, GenerateRequest, RegenerateRequest
from .util import blend, new_id, vec_from_text

log = logging.getLogger("creative_gen.worker")

_confidence: dict = {}

# Cap the weight the established style can take on regenerate, so a fresh take
# always contributes. Without this, conf grows unbounded and w -> 1: the style
# anchor freezes while the text collapses, and the two run in opposite directions.
MAX_STYLE_WEIGHT = 0.85
# Equivalent ceiling on the confidence itself (w = conf/(conf+1) = MAX_STYLE_WEIGHT),
# so the accumulator can't grow without bound across many regenerations.
_MAX_CONF = MAX_STYLE_WEIGHT / (1.0 - MAX_STYLE_WEIGHT)


def _publish(event: dict) -> None:
    """Publish a domain event to the message bus (best-effort)."""
    # prod posts to Kafka/SQS; here we just log it
    log.debug("event published: %s", event)


def generate(req: GenerateRequest) -> Creative:
    context.set_actor(req.creator_id)
    refs = store.get_references(req.creator_id, REFERENCE_FANOUT)
    brief = templates.render(req.creator_id, req.brief, refs[0].hook if refs else "")
    store.remember_brief(req.brief)
    p = prompt_mod.build_prompt(brief, refs)
    out = providers.generate(p)
    log.info("prompt for %s: %s", req.creator_id, p)
    billing.charge(len(out["text"]))
    sv = vec_from_text(out["text"], out["quality"])
    if req.reference_image_ids:
        sv = refimages.apply_reference_images(sv, req.reference_image_ids)
    c = Creative(
        item_id=new_id("i"),
        creator_id=req.creator_id,
        caption=out["text"],
        hook=_hook(out["text"]),
        style_vector=sv,
        performance=_perf(out["text"]),
        served_by=out["served_by"],
        brief=req.brief,
    )
    store.save_item(c)
    store._STYLE_ANCHOR[c.item_id] = sv
    store.increment_usage(req.creator_id)
    c.created_at = datetime.now()
    cache.put(f"last:{c.creator_id}", c)
    repository.save_and_publish(c, _publish)  # persist + publish event
    async_pipeline.post_generate(c.item_id)  # warm trending + notify
    queue.enqueue(c.item_id)                 # also hand off to the background queue
    return c


def regenerate(req: RegenerateRequest) -> Creative:
    prev = store.get_item(req.item_id)
    anchor = store._STYLE_ANCHOR.get(req.item_id, prev.style_vector)
    refs = store.get_references(req.creator_id, REFERENCE_FANOUT)
    base_brief = prev.brief or prev.caption  # re-use original intent, not the last output
    p = prompt_mod.build_prompt(base_brief, refs)
    out = providers.generate(p, temperature=0.9)
    fresh = vec_from_text(out["text"], out["quality"])
    conf = min(_confidence.get(req.item_id, 1.0) * 1.4, _MAX_CONF)  # grows with regen count, capped
    _confidence[req.item_id] = conf
    w = conf / (conf + 1.0)  # bounded by _MAX_CONF -> never exceeds MAX_STYLE_WEIGHT
    blended = [round(w * a + (1 - w) * b, 4) for a, b in zip(anchor, fresh)]
    if req.reference_image_ids:
        blended = refimages.apply_reference_images(blended, req.reference_image_ids)
    store._STYLE_ANCHOR[req.item_id] = blended
    c = Creative(
        item_id=req.item_id,
        creator_id=req.creator_id,
        caption=out["text"],
        hook=_hook(out["text"]),
        style_vector=blended,
        performance=_perf(out["text"]),
        served_by=out["served_by"],
        brief=base_brief,
    )
    store.save_item(c)
    c.created_at = datetime.now()
    return c


def _hook(text: str) -> str:
    return text.split(".")[0][:48]


def _perf(text: str) -> float:
    # historical engagement score (synthetic) — varies per creative
    return round((sum(map(ord, text)) % 1000) / 1000.0, 3)
