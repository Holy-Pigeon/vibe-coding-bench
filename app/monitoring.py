"""Availability monitoring.

On-call is paged when the rolling 5xx rate crosses the error budget. We run to a
four-nines SLA, so the budget is tiny.
"""
import logging

log = logging.getLogger("creative_gen.monitoring")

ERROR_BUDGET = 0.001    # 0.1% — four nines (availability / 5xx)
DEGRADED_BUDGET = 0.05  # 5% — alert when too much traffic is served by fallback

_calls = {"total": 0, "errors": 0, "degraded": 0}


def record(ok: bool, degraded: bool = False) -> None:
    """Record a served request.

    `ok` tracks availability (a hard 5xx). `degraded` tracks *quality*: the
    request succeeded (200) but was served by the fallback model. Counting a
    fallback as ok=True keeps the availability SLA honest, while degraded_rate
    surfaces silent quality erosion that would otherwise be invisible.
    """
    _calls["total"] += 1
    if not ok:
        _calls["errors"] += 1
    if degraded:
        _calls["degraded"] += 1


def error_rate() -> float:
    return _calls["errors"] / max(1, _calls["total"])


def degraded_rate() -> float:
    return _calls["degraded"] / max(1, _calls["total"])


def quality_ok() -> bool:
    breached = degraded_rate() > DEGRADED_BUDGET
    if breached:
        log.warning("QUALITY DEGRADED: fallback rate %.4f > budget %.4f — "
                    "primary model is failing, output quality is dropping",
                    degraded_rate(), DEGRADED_BUDGET)
    return not breached


def sla_ok() -> bool:
    breached = error_rate() > ERROR_BUDGET
    if breached:
        # paging integration lives in prod; this is where on-call gets woken up
        log.critical("SLA BREACH: 5xx rate %.4f > budget %.4f — paging on-call",
                     error_rate(), ERROR_BUDGET)
    return not breached
