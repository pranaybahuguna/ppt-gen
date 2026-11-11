"""The op language: a closed set of typed operations against a deck.

POC implements the subset needed for translate + create + repair. The full design
(~20 ops) extends this union; the executor dispatch table is the only other place
that changes.
"""
from __future__ import annotations

from typing import Annotated, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter


class _Op(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Expect(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text_sha: str


class SetParagraphText(_Op):
    """Rewrite a paragraph from tagged text; runs keep their own formatting and
    may be reordered or dropped (needed for translation - word order changes)."""
    op: Literal["set_paragraph_text"]
    target: str
    expect: Expect
    value: str


class SetRunText(_Op):
    op: Literal["set_run_text"]
    target: str
    expect: Expect
    value: str


class SetFontSize(_Op):
    """Explicit size on every run of a shape. Deterministic overflow repair."""
    op: Literal["set_font_size"]
    target: str
    size_pt: float = Field(ge=6, le=96)


class TextContent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["text"]
    text: str


class BulletsContent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["bullets"]
    items: list[str] = Field(min_length=1)


Content = Annotated[Union[TextContent, BulletsContent], Field(discriminator="kind")]


class AddSlide(_Op):
    op: Literal["add_slide"]
    layout: str                     # layout id from the template catalog, e.g. "M0L1"
    after: str | None = None        # slide ref; None = append
    fill: dict[int, Content]        # placeholder idx -> content (compiler resolved roles)
    notes: str | None = None
    drop_empty_placeholders: bool = True


class DeleteSlide(_Op):
    op: Literal["delete_slide"]
    target: str


class DeleteShape(_Op):
    op: Literal["delete_shape"]
    target: str


class SetGeometry(_Op):
    """Position/size in EMU. Emitted by the compiler, never by the model."""
    op: Literal["set_geometry"]
    target: str
    x: int = Field(ge=0)
    y: int = Field(ge=0)
    w: int = Field(gt=0)
    h: int = Field(ge=0)


# ---- create-from-pattern: copy an example slide, then edit the copy -----------------
# Steps inside AddSlideFromPattern address shapes on the NEW slide by id: "shape:<id>".

class SetShapeText(_Op):
    """Replace all text in a shape with these paragraphs. Each new paragraph copies the
    formatting (bullets, font, colour) of the example paragraph in the same position."""
    op: Literal["set_shape_text"]
    target: str
    paragraphs: list[str] = Field(min_length=1)


class DuplicateShape(_Op):
    """Copy a shape (all formatting) under a new id, so a pattern can have more items."""
    op: Literal["duplicate_shape"]
    source: str
    new_id: int = Field(gt=0)


class SetTable(_Op):
    """Rewrite a table: header row styled like the example header, body rows like the
    example body rows (alternating, so banding survives). Column count must match."""
    op: Literal["set_table"]
    target: str
    header: list[str]
    rows: list[list[str]]


class ChartSeries(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    values: list[float]


class SetChart(_Op):
    """Replace a chart-area shape with a real, editable PowerPoint chart (data sits in an
    embedded workbook, so people can edit it in PowerPoint). Numbers are put here by the
    compiler from a dataset - never typed by the model."""
    op: Literal["set_chart"]
    target: str
    chart_type: Literal["bar", "column", "stacked_column", "line", "pie", "doughnut"]
    categories: list[str] = Field(min_length=1, max_length=24)
    series: list[ChartSeries] = Field(min_length=1, max_length=6)
    title: str = ""
    number_format: str = "General"
    show_values: bool = True
    palette: list[str] = Field(min_length=1)
    font: str = "Calibri"
    text_colour: str = "374151"
    grid_colour: str = "E5E7EB"


Step = Annotated[
    Union[DuplicateShape, DeleteShape, SetGeometry, SetShapeText, SetTable, SetChart],
    Field(discriminator="op"),
]


class AddSlideFromPattern(_Op):
    op: Literal["add_slide_from_pattern"]
    source: str                 # the example slide to copy, e.g. "slide:258"
    steps: list[Step] = []
    notes: str | None = None


class SetDocProperties(_Op):
    """File > Properties. Always reset when a deck is built from someone else's file,
    so the old title/author/comments don't travel with the new deck."""
    op: Literal["set_doc_properties"]
    title: str = ""
    author: str = ""


Op = Annotated[
    Union[SetParagraphText, SetRunText, SetFontSize, AddSlide, DeleteSlide, DeleteShape, SetGeometry,
          AddSlideFromPattern, SetDocProperties],
    Field(discriminator="op"),
]
OpList = TypeAdapter(list[Op])
