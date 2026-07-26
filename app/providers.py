"""Model provider registry with a resilience fallback.

The primary model occasionally rate-limits or times out under load; we fall back
to a secondary model so the endpoint stays available.
"""
import logging
import os
import random
import time

from . import monitoring
from .config import FALLBACK_MODEL, PRIMARY_MODEL

log = logging.getLogger("creative_gen.providers")


class ProviderError(Exception):
    pass


def _load_pressure() -> float:
    """Fraction of primary calls that currently fail (rate-limit/timeout)."""
    return float(os.getenv("LOAD_PRESSURE", "0.35"))


def _slow_latency() -> float:
    """When the primary is degraded it hangs this long before erroring (seconds)."""
    return float(os.getenv("GEN_SLOW_S", "0.0"))


# ---- connection accounting (simulated) ------------------------------------
_open_clients = 0


class _Client:
    """A model-gateway connection. Prod opens a real TCP/HTTP connection."""

    def __init__(self):
        global _open_clients
        _open_clients += 1

    def close(self):
        global _open_clients
        _open_clients -= 1


def _render(prompt: str, quality: float, temperature: float) -> str:
    seed = sum(map(ord, prompt)) % 997
    tail = prompt.split(":")[-1].strip()[:48]
    text = f"[{seed}] {tail}"
    if quality < 0.8:
        # weaker model: more generic, drops the specific voice cues
        text = " ".join(w for w in text.split() if len(w) > 3)
    return text


class _Model:
    def __init__(self, name: str, quality: float):
        self.name = name
        self._quality = quality

    def generate(self, prompt: str, *, temperature: float) -> dict:
        # Real impl issues an HTTP call to the model gateway.
        if self.name == PRIMARY_MODEL and random.random() < _load_pressure():
            # degraded: hang for the full timeout before erroring (no fail-fast)
            time.sleep(_slow_latency())
            raise ProviderError(f"{self.name}: upstream 429 / timeout")
        return {
            "text": _render(prompt, self._quality, temperature),
            "served_by": self.name,
            "quality": self._quality,
        }


_PRIMARY = _Model(PRIMARY_MODEL, quality=1.0)
_FALLBACK = _Model(FALLBACK_MODEL, quality=0.62)


def generate(prompt: str, *, temperature: float = 0.7) -> dict:
    """Generate with the primary model; fall back on failure to stay available."""
    client = _Client()  # one connection per request
    try:
        out = _PRIMARY.generate(prompt, temperature=temperature)
        monitoring.record(ok=True)
        return out
    except Exception as e:  # noqa: BLE001 - stay resilient under load
        # per-call at info: the aggregate degraded_rate / quality_ok() is the alarm,
        # not each individual fallback (which would drown real errors under load).
        log.info("primary failed (%s); serving degraded fallback", e)
        out = _FALLBACK.generate(prompt, temperature=0.7)
        # request still succeeds (200), but flag the quality degradation so it
        # is visible in monitoring instead of hidden behind a green SLA.
        monitoring.record(ok=True, degraded=True)
        return out
    finally:
        client.close()  # release the connection instead of leaking it

