"""Model choice per task: a tier (simple/standard/complex) and/or a concrete model, resolved per agent.

The assigned agent maps tiers to models (`actors.model_tiers`), has a default model (`actors.model_default`) and an
allow list of models a task may name explicitly (`actors.models_allowed`). Resolution order: concrete model > tier
mapping > default model > nothing (the agent CLI's own default). Only runner agents (`APPLYING_KINDS`) apply the
model; Hermes, OpenClaw and desktop apps choose the model themselves and only show it.
"""
import json
import re

from app.errors import Unprocessable
from app.i18n import N_, _
from app.models import MODEL_TIERS, Actor, Task

# Agent kinds whose runner (`tbr run`) passes the model to the agent CLI.
APPLYING_KINDS = ("claude_code", "codex", "opencode")
TIER_LABELS = {"simple": N_("simple"), "standard": N_("standard"), "complex": N_("complex")}
# Model names as the agent CLIs take them, e.g. `sonnet`, `gpt-5.6-terra`, `ollama/qwen3:32b`, `opus[1m]`.
MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@+\[\]-]{0,99}$")
MAX_ALLOWED = 50


def tier_label(tier: str | None) -> str:
    return _(TIER_LABELS[tier]) if tier in TIER_LABELS else ""


def normalize_tier(value: str | None) -> str | None:
    value = (value or "").strip().lower()
    if not value or value == "none":
        return None
    if value not in MODEL_TIERS:
        raise Unprocessable(_("Model tier must be one of %(tiers)s", tiers=", ".join(MODEL_TIERS)),
                            code="invalid_model_tier")
    return value


def normalize_model(value: str | None) -> str | None:
    value = (value or "").strip()
    if not value or value.lower() == "none":
        return None
    if not MODEL_RE.match(value):
        raise Unprocessable(_("Invalid model name '%(model)s' (letters, digits and . _ - : / @ + [ ], max. 100 "
                              "characters)", model=value[:100]), code="invalid_model")
    return value


def _json(text: str | None, kind: type):
    try:
        value = json.loads(text) if text else kind()
    except ValueError:
        return kind()
    return value if isinstance(value, kind) else kind()


def tiers(actor: Actor | None) -> dict[str, str]:
    """Tier → model of an agent (only known tiers with a model)."""
    if actor is None:
        return {}
    raw = _json(actor.model_tiers, dict)
    return {t: str(raw[t]) for t in MODEL_TIERS if raw.get(t)}


def allowed(actor: Actor | None) -> list[str]:
    if actor is None:
        return []
    return [str(m) for m in _json(actor.models_allowed, list) if str(m).strip()]


def applies(actor: Actor | None) -> bool:
    return actor is not None and actor.actor_type == "agent" and actor.agent_kind in APPLYING_KINDS


def parse_list(text: str | list | None) -> list[str]:
    """Allow list from a form/API value: list, or text separated by commas, spaces or new lines."""
    items = text if isinstance(text, list) else re.split(r"[\s,;]+", text or "")
    out = list(dict.fromkeys(m for m in (normalize_model(str(i)) for i in items) if m))
    if len(out) > MAX_ALLOWED:
        raise Unprocessable(_("At most %(num)s allowed models", num=MAX_ALLOWED))
    return out


def set_agent_models(actor: Actor, *, default: str | None, tier_models: dict[str, str | None],
                     allowed_models: list[str] | str | None) -> list[str]:
    """Store an agent's model settings; returns the changed fields."""
    if actor.actor_type != "agent" and (default or any(tier_models.values()) or allowed_models):
        raise Unprocessable(_("Only agents have model settings"))
    unknown = set(tier_models) - set(MODEL_TIERS)
    if unknown:
        raise Unprocessable(_("Model tier must be one of %(tiers)s", tiers=", ".join(MODEL_TIERS)),
                            code="invalid_model_tier")
    mapping = {t: m for t in MODEL_TIERS if (m := normalize_model(tier_models.get(t)))}
    values = {"model_default": normalize_model(default),
              "model_tiers": json.dumps(mapping) if mapping else None,
              "models_allowed": json.dumps(parse_list(allowed_models)) if parse_list(allowed_models) else None}
    changed = []
    for key, value in values.items():
        if getattr(actor, key) != value:
            setattr(actor, key, value)
            changed.append(key)
    return changed


def settings(actor: Actor) -> dict:
    """An agent's model settings as returned by the API (`/agents/me`, admin)."""
    return {"default": actor.model_default, "tiers": tiers(actor), "allowed": allowed(actor),
            "applies": applies(actor)}


def check_allowed(actor: Actor | None, model: str | None) -> None:
    """A concrete model must be on the allow list of the assigned agent."""
    if not model:
        return
    if actor is None:
        raise Unprocessable(_("A concrete model needs an assigned agent (its allow list decides)"),
                            code="model_not_allowed")
    if model not in allowed(actor):
        names = ", ".join(allowed(actor)) or _("none")
        raise Unprocessable(_("Model '%(model)s' is not allowed for %(agent)s (allowed: %(allowed)s)", model=model,
                              agent=actor.name, allowed=names), code="model_not_allowed")


def resolve(task: Task, actor: Actor | None) -> dict:
    """Effective model for `task` with `actor`: {model, source (model|tier|default|None), applies}."""
    model, source = None, None
    if task.model:
        model, source = task.model, "model"
    elif task.model_tier and tiers(actor).get(task.model_tier):
        model, source = tiers(actor)[task.model_tier], "tier"
    elif actor is not None and actor.model_default:
        model, source = actor.model_default, "default"
    return {"model": model, "source": source, "applies": applies(actor)}
