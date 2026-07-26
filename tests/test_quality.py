"""Quality-regression tests for creative-gen.

These assert things the smoke tests deliberately do NOT: that the output is
*correct*, not merely present. Each test maps to a confirmed root cause of the
"output degrading" complaint. They are expected to be RED before the fix and
GREEN after.

Run offline (no DB):
    DATABASE_URL= python -m pytest tests/test_quality.py -q
"""
import os

import pytest

from app import monitoring, providers, refimages, store, worker
from app.models import Creative, GenerateRequest, RegenerateRequest


def _seed_item(item_id, creator_id):
    store.save_item(
        Creative(item_id=item_id, creator_id=creator_id, caption=f"cap-{item_id}",
                 hook=f"hook-{item_id}", style_vector=[0.0] * 8)
    )


# --- ① reference selection: anchor first, no cross-creator contamination -----

def test_references_keep_anchor_first_and_only_own_creator():
    for i in range(3):
        _seed_item(f"own{i}", "creator_A")   # own0 is the identity anchor
    for i in range(3):
        _seed_item(f"other{i}", "creator_B")

    refs = store.get_references("creator_A", 4)
    ids = [r.item_id for r in refs]

    assert ids[0] == "own0", f"anchor must stay first, got {ids}"
    foreign = [r.item_id for r in refs if r.creator_id != "creator_A"]
    assert foreign == [], f"no other creator's work may leak in, got {foreign}"


# --- ⑤ + ⑧ reference-image mode: deterministic, no shared-cache corruption ----

def test_reference_image_is_deterministic():
    refimages._HOT_CACHE.clear()
    base = [1.0] * 8
    first = refimages.apply_reference_images(base, ["img_det"])
    second = refimages.apply_reference_images(base, ["img_det"])
    third = refimages.apply_reference_images(base, ["img_det"])
    assert first == second == third, (
        f"same input must yield same output; got {first} / {second} / {third}"
    )


def test_reference_cache_not_mutated_by_apply():
    refimages._HOT_CACHE.clear()
    refimages.apply_reference_images([1.0] * 8, ["img_stable"])
    cached = refimages._HOT_CACHE[refimages._cache_key("img_stable")][0]
    fresh = refimages.fetch_style_hot("img_stable")  # served from cache
    assert cached == fresh
    assert max(cached) > 0.05, f"cached vector was decayed in place: {cached}"


def test_cache_key_resists_collision():
    # md5[:6] (24 bit) collides at ~4k entries; a full-width key must not.
    assert refimages._cache_key("img_5895") != refimages._cache_key("img_8821")


# --- ⑨ observability: degradation must be countable ---------------------------

def test_monitoring_sees_fallback_degradation(monkeypatch):
    monkeypatch.setenv("LOAD_PRESSURE", "1.0")  # force primary to always fail
    monitoring._calls.update(total=0, errors=0, degraded=0)
    for _ in range(20):
        out = providers.generate("p")
        assert out["served_by"] == providers.FALLBACK_MODEL  # served, but degraded
    assert monitoring.degraded_rate() == pytest.approx(1.0), (
        "100% fallback must be visible as degraded_rate, not hidden behind ok=True"
    )


def test_provider_does_not_leak_connections(monkeypatch):
    monkeypatch.setenv("LOAD_PRESSURE", "0.0")
    before = providers._open_clients
    for _ in range(50):
        providers.generate("p")
    assert providers._open_clients == before, "each request must close its client"


# --- ② + ④ regenerate: no self-collapse, weight stays bounded -----------------

def test_regenerate_preserves_original_brief(monkeypatch):
    monkeypatch.setenv("LOAD_PRESSURE", "0.0")  # isolate from fallback noise
    c = worker.generate(GenerateRequest(creator_id="creator_R", brief="zephyrbrief"))
    for _ in range(8):
        c = worker.regenerate(RegenerateRequest(creator_id="creator_R", item_id=c.item_id))
    assert "zephyrbrief" in c.caption, (
        f"original brief must survive repeated regeneration, got {c.caption!r}"
    )


def test_regenerate_weight_is_bounded(monkeypatch):
    monkeypatch.setenv("LOAD_PRESSURE", "0.0")
    c = worker.generate(GenerateRequest(creator_id="creator_W", brief="anchor test"))
    for _ in range(15):
        c = worker.regenerate(RegenerateRequest(creator_id="creator_W", item_id=c.item_id))
    conf = worker._confidence.get(c.item_id, 1.0)
    w = conf / (conf + 1.0)
    assert w <= worker.MAX_STYLE_WEIGHT + 1e-9, (
        f"prior-style weight must be capped so fresh style always contributes; w={w}"
    )
