"""Designer forms kept in the conversation until the designer's next message."""
from __future__ import annotations

import json
from typing import Annotated, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator, model_validator


Label = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]
Identifier = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]


class SelectionOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: Literal["single", "multiple", "box"]
    category: str | None = Field(default=None, max_length=100, pattern=r"^OST_[A-Za-z0-9_]+$")


class SelectionQuestion(SelectionOptions):
    prompt: Label


class Option(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)
    value: Label | int | float | bool
    label: Label

    @field_validator("value")
    @classmethod
    def browser_safe_integer(cls, value):
        if type(value) in (int, float) and abs(value) > 2**53 - 1:
            raise ValueError("integer option values must be in the browser's safe integer range")
        return value

    @property
    def key(self):
        # JSON in a browser has one number type (1.0 comes back as 1).
        kind = float if type(self.value) in (int, float) else type(self.value)
        return kind, self.value


class DesignerQuestion(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    id: Identifier
    text: Label
    options: list[Option] = Field(min_length=2, max_length=6)
    allow_other: bool = False
    multiple: bool = False

    @model_validator(mode="after")
    def distinct_values(self):
        if len({o.key for o in self.options}) != len(self.options):
            raise ValueError("each option must have a distinct value")
        return self


class DesignerForm(BaseModel):
    model_config = ConfigDict(extra="forbid")
    questions: list[DesignerQuestion] = Field(min_length=1, max_length=4)

    @model_validator(mode="after")
    def distinct_ids(self):
        if len({q.id for q in self.questions}) != len(self.questions):
            raise ValueError("each question must have a distinct id")
        return self

    def event(self):
        return {"ask_id": uuid4().hex, **self.model_dump()}


class AnswerItem(Option):
    id: Identifier


class DesignerAnswers(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ask_id: Identifier
    items: list[AnswerItem] = Field(min_length=1, max_length=28)

    def verified_items(self, event: dict | None) -> list[dict] | None:
        """Rebuild answers in question order from the options the host actually offered."""
        if not event or event["ask_id"] != self.ask_id:
            return None
        if {item.id for item in self.items} != {q["id"] for q in event["questions"]}:
            return None
        verified = []
        for q in event["questions"]:
            items = [i for i in self.items if i.id == q["id"]]
            if not q["multiple"] and len(items) != 1:
                return None
            offered = {Option.model_validate(o).key: o for o in q["options"]}
            has_free_text = False
            picked = set()
            for item in items:
                option = offered.get(item.key)
                if option is None or option["label"] != item.label:
                    if not q["allow_other"] or not isinstance(item.value, str) or item.value != item.label:
                        return None
                    # An older client sends Other as text, including for a numeric option.
                    option = next((o for o in q["options"] if o["label"] == item.value), option)
                    if option is None:
                        try:
                            scalar = json.loads(item.value)
                        except ValueError:
                            scalar = None
                        if type(scalar) in (int, float, bool):
                            key = bool if type(scalar) is bool else float
                            option = offered.get((key, scalar))
                    if option is None:
                        if has_free_text:
                            return None
                        verified.append({"id": q["id"], "label": item.label, "value": item.value, "other": True})
                        has_free_text = True
                        continue
                key = Option.model_validate(option).key
                if key not in picked:
                    picked.add(key)
                    verified.append({"id": q["id"], **option})
        return verified
