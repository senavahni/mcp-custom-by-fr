"""Port 1:1 `backend/src/shared/schemas/block.schema.ts` (BlockNote, contract 1.0).

`content_json = {props, content}`; properti tak dikenal DIBUANG (perilaku default z.object),
default diisi, teks dibersihkan dari karakter kontrol. Validasi dilakukan SEBELUM webhook dikirim
agar Dashboard tidak menolak event (400 VALIDATION_ERROR).
"""

from __future__ import annotations

import re
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit

import orjson
from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
    field_validator,
)

from mcp_marketing.utils.text import strip_control_chars

MAX_BLOCK_JSON = 65_536
MAX_INLINE = 2_000
MAX_TEXT = 10_000
MAX_TABLE_ROWS = 50
MAX_TABLE_COLS = 20

_COLOR_RE = r"^[a-zA-Z0-9#_-]*$"
_LANG_RE = r"^[a-zA-Z0-9+#._-]*$"


def is_safe_url(value: str, allow_mailto: bool = False) -> bool:
    if len(value) > 2048:
        return False
    try:
        parts = urlsplit(value)
    except ValueError:
        return False
    if parts.scheme in ("http", "https"):
        return bool(parts.netloc)
    return allow_mailto and parts.scheme == "mailto" and bool(parts.path)


def _clean(max_len: int) -> Any:
    return Annotated[
        str, StringConstraints(max_length=max_len), AfterValidator(strip_control_chars)
    ]


def _http_url(v: str) -> str:
    if v != "" and not is_safe_url(v):
        raise ValueError("URL harus http:// atau https://")
    return v


def _link_url(v: str) -> str:
    if not is_safe_url(v, True):
        raise ValueError("Link harus http(s):// atau mailto:")
    return v


Color = Annotated[str, StringConstraints(max_length=32, pattern=_COLOR_RE)]
Alignment = Literal["left", "center", "right", "justify"]
HttpUrl = Annotated[str, StringConstraints(max_length=2048), AfterValidator(_http_url)]
LinkUrl = Annotated[str, StringConstraints(max_length=2048), AfterValidator(_link_url)]


class _Strip(BaseModel):
    model_config = ConfigDict(extra="ignore")


class Styles(_Strip):
    bold: bool | None = None
    italic: bool | None = None
    underline: bool | None = None
    strike: bool | None = None
    code: bool | None = None
    textColor: Color | None = None  # noqa: N815 - nama field mengikuti BlockNote
    backgroundColor: Color | None = None  # noqa: N815


class StyledText(_Strip):
    type: Literal["text"]
    text: _clean(MAX_TEXT)  # type: ignore[valid-type]
    styles: Styles = Field(default_factory=Styles)


class Link(_Strip):
    type: Literal["link"]
    href: LinkUrl
    content: list[StyledText] = Field(max_length=200)


class MentionProps(_Strip):
    mention_type: Literal["user", "agent"]
    id: str
    label: _clean(100)  # type: ignore[valid-type]

    @field_validator("id")
    @classmethod
    def _uuid(cls, v: str) -> str:
        import uuid

        return str(uuid.UUID(v)).lower()

    @field_validator("label")
    @classmethod
    def _label_min(cls, v: str) -> str:
        if not v:
            raise ValueError("label minimal 1 karakter")
        return v


class Mention(_Strip):
    type: Literal["mention"]
    props: MentionProps


Inline = Annotated[StyledText | Link | Mention, Field(discriminator="type")]
InlineArray = Annotated[list[Inline], Field(max_length=MAX_INLINE)]
TextOnlyArray = Annotated[list[StyledText], Field(max_length=MAX_INLINE)]


class TextProps(_Strip):
    textColor: Color | None = None  # noqa: N815
    backgroundColor: Color | None = None  # noqa: N815
    textAlignment: Alignment | None = None  # noqa: N815


class HeadingProps(TextProps):
    level: Literal[1, 2, 3] = 1
    isToggleable: bool | None = None  # noqa: N815


class NumberedProps(TextProps):
    start: int | None = Field(default=None, ge=1, le=100_000)


class CheckProps(TextProps):
    checked: bool = False


class CodeProps(_Strip):
    language: Annotated[str, StringConstraints(max_length=32, pattern=_LANG_RE)] | None = None


class TableCellProps(_Strip):
    backgroundColor: Color | None = None  # noqa: N815
    textColor: Color | None = None  # noqa: N815
    textAlignment: Alignment | None = None  # noqa: N815
    colspan: int | None = Field(default=None, ge=1, le=20)
    rowspan: int | None = Field(default=None, ge=1, le=50)


class TableCell(_Strip):
    type: Literal["tableCell"]
    props: TableCellProps = Field(default_factory=TableCellProps)
    content: InlineArray = Field(default_factory=list)


class TableRow(_Strip):
    cells: list[InlineArray | TableCell] = Field(max_length=MAX_TABLE_COLS)


class TableContent(_Strip):
    type: Literal["tableContent"]
    columnWidths: list[Annotated[float, Field(gt=0, le=4000)] | None] | None = Field(  # noqa: N815
        default=None, max_length=MAX_TABLE_COLS
    )
    headerRows: int | None = Field(default=None, ge=0, le=1)  # noqa: N815
    headerCols: int | None = Field(default=None, ge=0, le=1)  # noqa: N815
    rows: list[TableRow] = Field(max_length=MAX_TABLE_ROWS)


class TableProps(_Strip):
    textColor: Color | None = None  # noqa: N815
    backgroundColor: Color | None = None  # noqa: N815


class ImageProps(_Strip):
    url: HttpUrl = ""
    caption: _clean(1000) | None = None  # type: ignore[valid-type]
    name: _clean(255) | None = None  # type: ignore[valid-type]
    previewWidth: float | None = Field(default=None, gt=0, le=4000)  # noqa: N815
    showPreview: bool | None = None  # noqa: N815
    textAlignment: Alignment | None = None  # noqa: N815
    backgroundColor: Color | None = None  # noqa: N815


class BookmarkProps(_Strip):
    url: HttpUrl = ""
    title: _clean(300) | None = None  # type: ignore[valid-type]
    description: _clean(1000) | None = None  # type: ignore[valid-type]


_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)


class AiPromptProps(_Strip):
    prompt: _clean(4000) = ""  # type: ignore[valid-type]
    agent_id: str | None = None
    status: Literal["idle", "running", "completed", "failed"] = "idle"
    last_run_id: str | None = None

    @field_validator("agent_id", "last_run_id")
    @classmethod
    def _uuid_or_empty(cls, v: str | None) -> str | None:
        if v is None or v == "" or _UUID_RE.match(v):
            return v
        raise ValueError("harus UUID atau kosong")


def _with_content(
    props: type[BaseModel], content: Any, *, content_default: Any = list
) -> type[BaseModel]:
    fields: dict[str, Any] = {"props": (props, Field(default_factory=props))}
    if content_default is None:
        fields["content"] = (content | None, None)
    else:
        fields["content"] = (content, Field(default_factory=content_default))
    return _make(f"{props.__name__}Block", fields)


def _props_only(props: type[BaseModel]) -> type[BaseModel]:
    return _make(f"{props.__name__}Only", {"props": (props, Field(default_factory=props))})


def _make(name: str, fields: dict[str, Any]) -> type[BaseModel]:
    from pydantic import create_model

    return create_model(name, __base__=_Strip, **fields)  # type: ignore[call-overload,no-any-return]


class _Empty(_Strip):
    pass


BLOCK_SCHEMAS: dict[str, type[BaseModel]] = {
    "paragraph": _with_content(TextProps, InlineArray),
    "heading": _with_content(HeadingProps, InlineArray),
    "toggleListItem": _with_content(TextProps, InlineArray),
    "bulletListItem": _with_content(TextProps, InlineArray),
    "numberedListItem": _with_content(NumberedProps, InlineArray),
    "checkListItem": _with_content(CheckProps, InlineArray),
    "quote": _with_content(TextProps, InlineArray),
    "codeBlock": _with_content(CodeProps, TextOnlyArray),
    "table": _with_content(TableProps, TableContent, content_default=None),
    "image": _props_only(ImageProps),
    "divider": _props_only(_Empty),
    "bookmark": _props_only(BookmarkProps),
    "aiPrompt": _props_only(AiPromptProps),
}
BLOCK_TYPES = tuple(BLOCK_SCHEMAS)


class BlockValidationError(ValueError):
    def __init__(self, issues: list[dict[str, str]]) -> None:
        super().__init__("; ".join(f"{i['path']}: {i['message']}" for i in issues[:5]))
        self.issues = issues


def validate_block_content(block_type: str, content_json: Any) -> dict[str, Any]:
    """Validasi & bersihkan content_json; kembalikan versi bersih (setara output Zod)."""
    schema = BLOCK_SCHEMAS.get(block_type)
    if schema is None:
        raise BlockValidationError(
            [{"path": "block_type", "message": f"block_type tidak dikenal: {block_type}"}]
        )
    try:
        parsed = schema.model_validate(content_json if content_json is not None else {})
    except ValidationError as exc:
        raise BlockValidationError(
            [
                {"path": "content_json." + ".".join(str(p) for p in e["loc"]), "message": e["msg"]}
                for e in exc.errors()
            ]
        ) from None
    cleaned = parsed.model_dump(mode="json", exclude_none=True)
    if len(orjson.dumps(cleaned)) > MAX_BLOCK_JSON:
        raise BlockValidationError(
            [{"path": "content_json", "message": f"melebihi {MAX_BLOCK_JSON} byte"}]
        )
    return cleaned  # type: ignore[no-any-return]


def text_inline(text: str, **styles: bool) -> dict[str, Any]:
    return {"type": "text", "text": text, "styles": dict(styles)}


def _inline_to_text(items: Any) -> str:
    if not isinstance(items, list):
        return ""
    out = []
    for i in items:
        if not isinstance(i, dict):
            continue
        if i.get("type") == "text":
            out.append(str(i.get("text", "")))
        elif i.get("type") == "link":
            out.append("".join(str(c.get("text", "")) for c in i.get("content", [])))
        elif i.get("type") == "mention":
            out.append("@" + str(i.get("props", {}).get("label", "")))
    return "".join(out)


def extract_plain_text(block_type: str, content: dict[str, Any]) -> str:
    """Port `extractPlainText` Dashboard (untuk outline hemat token)."""
    props = content.get("props") or {}
    if block_type == "table":
        rows = (content.get("content") or {}).get("rows", [])
        return "\n".join(
            " | ".join(
                _inline_to_text(c if isinstance(c, list) else c.get("content"))
                for c in r.get("cells", [])
            )
            for r in rows
        )
    if block_type == "image":
        return " ".join(str(v) for v in (props.get("caption"), props.get("name")) if v)
    if block_type == "bookmark":
        return " ".join(
            str(v) for v in (props.get("title"), props.get("description"), props.get("url")) if v
        )
    if block_type == "aiPrompt":
        return str(props.get("prompt", ""))
    if block_type == "divider":
        return ""
    return _inline_to_text(content.get("content"))
