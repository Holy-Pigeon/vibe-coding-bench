"""Request authentication.

Every endpoint requires a valid API key. Keys map to a tenant (VIZ-0904), and
that tenant scopes every read/write so one tenant can never see another's work.

Keys are loaded from the environment, never hardcoded:
  - API_KEYS="key1:tenant_a,key2:tenant_b"  (production: one key per tenant)
  - API_KEY="..."                            (single-tenant convenience)
Falls back to a demo key only when neither is set, so local/offline dev works.
"""
import os
import secrets
from typing import Dict

from fastapi import Header, HTTPException


def _load_keys() -> Dict[str, str]:
    mapping: Dict[str, str] = {}
    for pair in os.getenv("API_KEYS", "").split(","):
        pair = pair.strip()
        if not pair:
            continue
        key, _, tenant = pair.partition(":")
        if key.strip() and tenant.strip():
            mapping[key.strip()] = tenant.strip()
    if not mapping:
        # dev/offline fallback: single key -> single tenant
        mapping[os.getenv("API_KEY", "sk_live_demo")] = "tenant_default"
    return mapping


_KEYS = _load_keys()


def require_api_key(x_api_key: str = Header(default="")) -> str:
    """Resolve the caller's API key to its tenant, or 401.

    The returned tenant is threaded through the request so every data access is
    scoped to it — this is what closes the cross-tenant read (IDOR).
    """
    for key, tenant in _KEYS.items():
        # constant-time compare so key validity isn't leaked by timing
        if secrets.compare_digest(x_api_key, key):
            return tenant
    raise HTTPException(status_code=401, detail="invalid api key")
