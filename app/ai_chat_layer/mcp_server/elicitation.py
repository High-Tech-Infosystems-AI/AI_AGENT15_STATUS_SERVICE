"""Server-side elicitation primitives.

Tools call `make_elicitation(...)` (or raise `ElicitationRequired`) when
they need the user to disambiguate an argument. The MCP client wrapper
detects the special return shape, surfaces the spec to the chat UI as an
`ai_elicitation` ref, and returns a "pending" payload to the model so
the model writes a brief acknowledgment instead of looping.

Wire format (what gets stored in the chat message ref's `params`):

    {
      "id": "<uuid12>",
      "title": "...",
      "intro": "...",
      "fields": [
        {"name", "label", "kind", "options": [{value,label,description}],
         "required", "placeholder", "default"}
      ],
      "submit_label": "Submit"
    }
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Literal, Optional

FieldKind = Literal[
    "select", "multiselect", "text", "number", "date", "buttons",
]


@dataclass
class ElicitationOption:
    value: str
    label: Optional[str] = None
    description: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "value": self.value,
            "label": self.label,
            "description": self.description,
        }


@dataclass
class ElicitationField:
    name: str
    label: str
    kind: FieldKind = "text"
    options: List[ElicitationOption] = field(default_factory=list)
    required: bool = True
    placeholder: Optional[str] = None
    default: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "label": self.label,
            "kind": self.kind,
            "required": self.required,
            "placeholder": self.placeholder,
            "default": self.default,
            "options": [o.to_dict() for o in self.options],
        }


@dataclass
class ElicitationSpec:
    title: str
    fields: List[ElicitationField]
    intro: Optional[str] = None
    submit_label: str = "Submit"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": uuid.uuid4().hex[:12],
            "title": self.title,
            "intro": self.intro,
            "fields": [f.to_dict() for f in self.fields],
            "submit_label": self.submit_label,
        }


def make_elicitation(spec: ElicitationSpec, *, note: Optional[str] = None) -> Dict[str, Any]:
    """Build the dict shape data tools return when they need user input.

    The tool wrapper recognizes the top-level `elicitation_required` key
    and forwards the spec to the chat as an `ai_elicitation` ref.
    """
    return {
        "elicitation_required": spec.to_dict(),
        "note": note or (
            "Awaiting user input via inline form. The user's answer will "
            "arrive as the next turn with `[elicit:<id>] {...json...}`."
        ),
    }


class ElicitationRequired(Exception):
    """Alternative to returning the dict — raise this from anywhere inside
    a tool implementation and the wrapper converts it to the same shape."""

    def __init__(self, spec: ElicitationSpec, note: Optional[str] = None):
        self.spec = spec
        self.note = note
        super().__init__(note or "elicitation required")
