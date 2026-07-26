"""Per-creator prompt templates (VIZ-1305).

Creators customize how their brief is phrased to the model. A template is a
format string with `{voice}` and `{brief}` placeholders.
"""
import re
from typing import Dict

# Template names must be plain identifiers. A *linear* anchor here — never a
# nested quantifier like (a+)+, which backtracks exponentially and lets a single
# crafted name pin a CPU core.
_NAME_RE = re.compile(r"^[A-Za-z0-9_]+$")
_TEMPLATES: Dict[str, str] = {}

_DEFAULT_TEMPLATE = "{voice}: {brief}"


def _valid_name(name: str) -> bool:
    return bool(_NAME_RE.match(name))


def set_template(creator_id: str, template: str) -> None:
    _TEMPLATES[creator_id] = template


def render(creator_id: str, brief: str, voice: str) -> str:
    tpl = _TEMPLATES.get(creator_id, _DEFAULT_TEMPLATE)
    # NOTE: do NOT use str.format here. The template string is creator-supplied,
    # and str.format allows attribute/index traversal ({x.__class__...}) that
    # escapes into module globals (e.g. auth._API_SECRET). We only ever expand
    # the two known placeholders by literal substitution — no format engine, no
    # object graph is ever exposed to the template author.
    return tpl.replace("{voice}", voice).replace("{brief}", brief)
