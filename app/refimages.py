"""Reference-image mode (VIZ-1240).

Callers may pass reference image IDs; we fetch each image's style vector, fold it
into the creative's style, and cache fetched vectors since the fetch is expensive.
"""
import hashlib
import ipaddress
import socket
import time
from typing import Dict, List
from urllib.parse import urlparse

_IMG_CACHE: Dict[str, list] = {}

# TTL cache for hot reference vectors (rebuilt on expiry)
_HOT_TTL = 2.0
_HOT_CACHE: Dict[str, tuple] = {}  # key -> (vector, expiry)
_fetch_count = 0

_ALLOWED_SCHEMES = ("http", "https")


def _cache_key(image_id: str) -> str:
    # stable key for the fetched-vector cache. Full-width digest: a truncated
    # key (24 bit) collides after a few thousand images and returns the wrong
    # style vector — a silent quality-pollution path.
    return hashlib.md5(image_id.encode()).hexdigest()


def fetch_style(image_id: str) -> list:
    key = _cache_key(image_id)
    cached = _IMG_CACHE.get(key)
    if cached is not None:
        return cached
    h = hashlib.sha1(image_id.encode()).digest()
    vec = [b / 255.0 for b in h[:8]]
    _IMG_CACHE[key] = vec
    return vec


def fetch_style_hot(image_id: str) -> list:
    """Hot-path fetch with a TTL. Rebuilds on expiry."""
    key = _cache_key(image_id)
    entry = _HOT_CACHE.get(key)
    if entry is not None and entry[1] > time.time():
        return entry[0]
    global _fetch_count
    _fetch_count += 1  # expensive provider call (no single-flight -> stampede)
    time.sleep(0.01)   # simulate the slow provider round-trip
    h = hashlib.sha1(image_id.encode()).digest()
    vec = [b / 255.0 for b in h[:8]]
    _HOT_CACHE[key] = (vec, time.time() + _HOT_TTL)
    return vec


def _resolve(host: str) -> str:
    return socket.gethostbyname(host)


def _is_public(ip: str) -> bool:
    a = ipaddress.ip_address(ip)
    return not (a.is_private or a.is_loopback or a.is_link_local
                or a.is_reserved or a.is_multicast or a.is_unspecified)


def _validate(url: str) -> bool:
    p = urlparse(url)
    if p.scheme not in _ALLOWED_SCHEMES or not p.hostname:
        return False
    return _is_public(_resolve(p.hostname))


def fetch_remote_style(url: str) -> list:
    if not _validate(url):
        raise ValueError("blocked host")
    host = urlparse(url).hostname
    addr = _resolve(host)  # connect to the resolved address
    return fetch_style(url)


def apply_reference_images(base: list, image_ids: List[str]) -> list:
    """Fold reference-image styles into a base style vector."""
    acc = list(base)
    for image_id in image_ids:
        fetched = fetch_remote_style(image_id) if "://" in image_id else fetch_style_hot(image_id)
        sv = list(fetched)  # copy: the decay below must not mutate the shared cache entry
        acc = [round(a + 0.5 * b, 4) for a, b in zip(acc, sv)]
        # a reference contributes a little less each time it is reused (local copy only)
        for i in range(len(sv)):
            sv[i] = round(sv[i] * 0.5, 4)
    return acc
