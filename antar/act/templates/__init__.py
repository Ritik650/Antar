"""The template registry. **The LLM fills slots; it cannot emit a body.**

This package is the mechanical form of N1. `registry.yaml` holds fixed message bodies
with named, typed placeholders; a draft is a mapping from slot name to value; rendering
is `str.format`. A model that returns free text has returned a field nothing reads.

Mirrors India's DLT regime, where every commercial template is pre-registered against a
header and delivered content must match the registered body. **None of these templates
is actually registered** - `dlt_template_id` values are placeholders in the right shape.
docs/LIMITATIONS.md L11.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from antar.signals.schemas import Channel, MessageClass

REGISTRY_PATH = Path(__file__).with_name("registry.yaml")

# Slot types. Each is a validator, not a suggestion: a value that fails its type is
# rejected before the gate sees it, so a model cannot smuggle a URL into a name field.
SLOT_VALIDATORS: dict[str, re.Pattern[str]] = {
    # Letters, digits, spaces and a few punctuation marks a real merchant name uses.
    # Deliberately excludes ':' and '/' so a name cannot become a link.
    "name": re.compile(r"^[\w \.\-&',()]{1,60}$", re.UNICODE),
    # Digits with optional grouping and decimals. No currency symbol: the template
    # supplies "Rs", so a model cannot restate it as something else.
    "money": re.compile(r"^\d{1,3}(,\d{2,3})*(\.\d{1,2})?$|^\d+(\.\d{1,2})?$"),
    "date": re.compile(r"^\d{1,2} [A-Z][a-z]{2} \d{4}$"),
    # HTTPS only, and only on hosts we own. A model that returns any other host has
    # returned a value this rejects - which is the point.
    "url": re.compile(r"^https://(pay|link|auth)\.antar\.example/[A-Za-z0-9_\-/]{1,64}$"),
    # A short human phrase. No digits, so an amount cannot be restated here either,
    # and no URL characters.
    "reason_phrase": re.compile(r"^[A-Za-z][A-Za-z \-,']{2,79}$"),
}


class TemplateError(Exception):
    """A draft that does not fit its template. Always a rejection, never a warning."""


@dataclass(frozen=True)
class Slot:
    name: str
    type: str
    max_length: int = 120

    def validate(self, value: Any) -> str:
        if not isinstance(value, str):
            raise TemplateError(f"slot {self.name!r} must be a string, got {type(value).__name__}")
        text = value.strip()
        if not text:
            raise TemplateError(f"slot {self.name!r} is empty")
        if len(text) > self.max_length:
            raise TemplateError(
                f"slot {self.name!r} is {len(text)} characters, over the {self.max_length} limit"
            )
        pattern = SLOT_VALIDATORS.get(self.type)
        if pattern is None:
            raise TemplateError(f"slot {self.name!r} has unknown type {self.type!r}")
        if not pattern.match(text):
            raise TemplateError(
                f"slot {self.name!r} value {text[:40]!r} does not match the {self.type} format"
            )
        return text


@dataclass(frozen=True)
class Template:
    id: str
    dlt_template_id: str
    channel: Channel
    message_class: MessageClass
    language: str
    header: str
    body: str
    slots: dict[str, Slot] = field(default_factory=dict)

    @property
    def slot_names(self) -> frozenset[str]:
        return frozenset(self.slots)

    @property
    def placeholders(self) -> frozenset[str]:
        return frozenset(re.findall(r"\{(\w+)\}", self.body))

    def render(self, values: dict[str, Any]) -> str:
        """Validate every slot and substitute. Extra keys are a rejection, not noise.

        An unexpected key means the model invented a field, and a model inventing
        fields is a model that has misunderstood its job - better to fail loudly here
        than to silently drop whatever it thought it was adding.
        """
        provided = set(values)
        expected = set(self.slots)
        if missing := expected - provided:
            raise TemplateError(f"{self.id}: missing slots {sorted(missing)}")
        if extra := provided - expected:
            raise TemplateError(
                f"{self.id}: unexpected slots {sorted(extra)}. The template defines "
                f"{sorted(expected)} and nothing else."
            )
        clean = {name: self.slots[name].validate(values[name]) for name in expected}
        return " ".join(self.body.format(**clean).split())


@lru_cache(maxsize=1)
def load_templates(path: Path | None = None) -> dict[str, Template]:
    raw = yaml.safe_load((path or REGISTRY_PATH).read_text(encoding="utf-8"))
    out: dict[str, Template] = {}
    for entry in raw["templates"]:
        slots = {
            name: Slot(name=name, type=spec["type"], max_length=spec.get("max_length", 120))
            for name, spec in entry["slots"].items()
        }
        template = Template(
            id=entry["id"],
            dlt_template_id=entry["dlt_template_id"],
            channel=Channel(entry["channel"]),
            message_class=MessageClass(entry["message_class"]),
            language=entry["language"],
            header=entry["header"],
            body=" ".join(entry["body"].split()),
            slots=slots,
        )
        if template.placeholders != template.slot_names:
            raise TemplateError(
                f"{template.id}: body placeholders {sorted(template.placeholders)} do not "
                f"match declared slots {sorted(template.slot_names)}"
            )
        out[template.id] = template
    return out


def get(template_id: str) -> Template:
    templates = load_templates()
    try:
        return templates[template_id]
    except KeyError:
        raise TemplateError(
            f"unknown template {template_id!r}; registered: {sorted(templates)}"
        ) from None


def for_channel(channel: Channel) -> list[Template]:
    return [t for t in load_templates().values() if t.channel is channel]


__all__ = ["Slot", "Template", "TemplateError", "for_channel", "get", "load_templates"]
