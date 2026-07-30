"""Load Discord embeds from named YAML message files."""

from __future__ import annotations

import math
import re
import string
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, TypedDict, TypeGuard, override

import yaml
from discord import Embed
from noctalia_i18n_core import SOURCE_LOCALE, Action
from yaml.constructor import ConstructorError
from yaml.nodes import MappingNode, Node
from yaml.resolver import BaseResolver

from .diff import AnsiStyle, format_ansi_diff

type _YamlValue = (
    None | bool | int | float | str | list[_YamlValue] | dict[str, _YamlValue]
)


class MessageValues(TypedDict):
    """Values available to detailed and merged message templates."""

    monitor_id: str
    project: str
    key: str
    source: str | None
    old_value: str | None
    new_value: str | None
    locale: str
    actor: str
    actor_url: str | None
    actor_avatar_url: str | None
    action: Action
    change_url: str | None
    timestamp: str
    unix_time: int


class _MergeValues(TypedDict):
    monitor_id: str
    project: str
    locale: str
    count: int
    actors: str
    actor_count: int
    actor_avatar_url: str | None
    added_count: int
    modified_count: int
    deleted_count: int
    first_timestamp: str
    last_timestamp: str
    first_unix_time: int
    last_unix_time: int
    entries: str


type _DiffStyles = tuple[AnsiStyle, AnsiStyle]


@dataclass(frozen=True, slots=True)
class _TemplateScope:
    placeholders: frozenset[str]
    truncatable: frozenset[str]
    fallbackable: frozenset[str]


_ACTIONS: tuple[Action, ...] = ("added", "modified", "deleted")
_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_TRUNCATE_SPEC = re.compile(r"^truncate=([1-9]\d*)$")
_FALLBACK_SPEC = re.compile(r"^fallback=([^{}]+)$")
_DIFF_PLACEHOLDERS = frozenset({"old_diff", "new_diff"})
_MESSAGE_PLACEHOLDERS = (
    frozenset(MessageValues.__required_keys__) | _DIFF_PLACEHOLDERS | {"key_link"}
)
_MERGE_PLACEHOLDERS = frozenset(_MergeValues.__required_keys__)
_MESSAGE_SCOPE = _TemplateScope(
    _MESSAGE_PLACEHOLDERS,
    frozenset({"key", "source", "old_value", "new_value", "actor"}),
    _MESSAGE_PLACEHOLDERS - {"unix_time"},
)
_MERGE_SCOPE = _TemplateScope(
    _MERGE_PLACEHOLDERS,
    frozenset({"actors", "entries"}),
    _MERGE_PLACEHOLDERS
    - {
        "count",
        "actor_count",
        "added_count",
        "modified_count",
        "deleted_count",
        "first_unix_time",
        "last_unix_time",
    },
)
_EMBED_KEYS = frozenset(
    {
        "title",
        "description",
        "url",
        "timestamp",
        "color",
        "footer",
        "image",
        "thumbnail",
        "author",
        "fields",
    }
)
_TEXT_LIMITS = {
    "title": 256,
    "description": 4096,
    "field.name": 256,
    "field.value": 1024,
    "footer.text": 2048,
    "author.name": 256,
}
_MAX_COLOR_VALUE = 0xFFFFFF
_MAX_EMBED_TEXT = 6000
_MAX_EMBED_FIELDS = 25
_OBJECT_SCHEMAS = {
    "footer": (frozenset({"text", "icon_url"}), frozenset({"text"})),
    "image": (frozenset({"url"}), frozenset({"url"})),
    "thumbnail": (frozenset({"url"}), frozenset({"url"})),
    "author": (frozenset({"name", "url", "icon_url"}), frozenset({"name"})),
}


def _is_list(value: object) -> TypeGuard[list[object]]:
    return isinstance(value, list)


def _is_dict(value: object) -> TypeGuard[dict[object, object]]:
    return isinstance(value, dict)


def _normalize(value: object, label: str) -> _YamlValue:
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{label} contains a non-finite number")
        return value
    if _is_list(value):
        return [
            _normalize(item, f"{label}[{index}]") for index, item in enumerate(value)
        ]
    if _is_dict(value):
        output: dict[str, _YamlValue] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError(f"{label} must use string mapping keys")
            output[key] = _normalize(item, f"{label}.{key}")
        return output
    raise ValueError(f"{label} contains an unsupported YAML value")


def _truncate(value: str, limit: int) -> str:
    widths = tuple(
        0
        if unicodedata.combining(character)
        else 2
        if unicodedata.east_asian_width(character) in {"F", "W"}
        else 1
        for character in value
    )
    if sum(widths) <= limit:
        return value
    width = 0
    end = 0
    for character_width in widths:
        if width + character_width > limit - 1:
            break
        width += character_width
        end += 1
    return value[:end].rstrip() + "…"


class _TemplateFormatter(string.Formatter):
    @override
    def format_field(self, value: object, format_spec: str) -> str:
        if not format_spec:
            return super().format_field(value, format_spec)
        fallback = _FALLBACK_SPEC.fullmatch(format_spec)
        if fallback is not None and isinstance(value, str):
            return value or fallback.group(1)
        truncate = _TRUNCATE_SPEC.fullmatch(format_spec)
        if truncate is None or not isinstance(value, str):
            raise ValueError(f"Invalid template format: {format_spec!r}")
        return _truncate(value, int(truncate.group(1)))


_FORMATTER = _TemplateFormatter()


class _ObjectLoader(Protocol):
    def construct_object(self, node: Node, deep: bool = False) -> object: ...


class _UniqueKeyLoader(yaml.SafeLoader):
    pass


def _construct_mapping(
    loader: _ObjectLoader,
    node: MappingNode,
    deep: bool = False,
) -> dict[object, object]:
    mapping: dict[object, object] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            duplicate = key in mapping
        except TypeError as exc:
            raise ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                "mapping keys must be scalar values",
                key_node.start_mark,
            ) from exc
        if duplicate:
            raise ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                f"duplicate key: {key!r}",
                key_node.start_mark,
            )
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_UniqueKeyLoader.add_constructor(BaseResolver.DEFAULT_MAPPING_TAG, _construct_mapping)


@dataclass(frozen=True, slots=True)
class MessageTemplate:
    """One selectable file containing detailed message templates."""

    name: str
    templates: Mapping[Action, dict[str, _YamlValue]]
    diff_styles: _DiffStyles | None

    def render(self, action: Action, values: Mapping[str, object]) -> Embed:
        """Render one detailed Discord embed."""

        try:
            template = self.templates[action]
        except KeyError:
            raise ValueError(
                f"Message {self.name!r} has no template for action {action!r}"
            ) from None
        label = f"message {self.name!r} action {action!r}"
        scope = _render_values(values, f"{label} values", self.diff_styles)
        return _render_embed(template, scope, label)


@dataclass(frozen=True, slots=True)
class MergePage:
    """One merged embed and the number of entries it contains."""

    count: int
    embed: Embed

    def __post_init__(self) -> None:
        if type(self.count) is not int or self.count < 1:
            raise ValueError("Merge page count must be a positive integer")


@dataclass(frozen=True, slots=True)
class MergeTemplate:
    """One selectable merged-message template."""

    name: str
    source: dict[str, _YamlValue]
    target: dict[str, _YamlValue]
    entries: Mapping[Action, str]
    separator: str
    diff_styles: _DiffStyles | None

    def render(self, values: Sequence[MessageValues]) -> tuple[MergePage, ...]:
        """Render a locale batch, splitting it at Discord embed limits."""

        if not values:
            raise ValueError("A merge requires at least one delivery")
        if len({item["locale"] for item in values}) != 1:
            raise ValueError("A merge cannot mix locales")
        if len({(item["monitor_id"], item["project"]) for item in values}) != 1:
            raise ValueError("A merge cannot mix monitor contexts")

        pages: list[MergePage] = []
        current: list[MessageValues] = []
        rendered: Embed | None = None
        for item in values:
            candidate = (*current, item)
            try:
                candidate_embed = self._render_page(candidate)
            except ValueError as exc:
                if not current or rendered is None:
                    raise ValueError(
                        f"Merged change cannot fit in one Discord embed: {exc}"
                    ) from exc
                pages.append(MergePage(len(current), rendered))
                current = [item]
                try:
                    rendered = self._render_page((item,))
                except ValueError as single_exc:
                    raise ValueError(
                        f"Merged change cannot fit in one Discord embed: {single_exc}"
                    ) from single_exc
            else:
                current.append(item)
                rendered = candidate_embed

        if current and rendered is not None:
            pages.append(MergePage(len(current), rendered))
        return tuple(pages)

    def _render_page(self, values: Sequence[MessageValues]) -> Embed:
        label = f"message {self.name!r} merge"
        entries: list[str] = []
        for index, item in enumerate(values):
            entry_scope = _render_values(
                item,
                f"{label} entry {index} values",
                self.diff_styles,
            )
            rendered = _render_string(self.entries[item["action"]], entry_scope)
            if not rendered:
                raise ValueError(f"{label} entry {index} must render as non-empty text")
            entries.append(rendered)

        actors = tuple(dict.fromkeys(item["actor"] for item in values))
        actor_avatar_url = (
            next(
                (
                    item["actor_avatar_url"]
                    for item in reversed(values)
                    if item["actor_avatar_url"]
                ),
                None,
            )
            if len(actors) == 1
            else None
        )
        first, last = values[0], values[-1]
        scope: _MergeValues = {
            "monitor_id": last["monitor_id"],
            "project": last["project"],
            "locale": last["locale"],
            "count": len(values),
            "actors": ", ".join(actors),
            "actor_count": len(actors),
            "actor_avatar_url": actor_avatar_url,
            "added_count": sum(item["action"] == "added" for item in values),
            "modified_count": sum(item["action"] == "modified" for item in values),
            "deleted_count": sum(item["action"] == "deleted" for item in values),
            "first_timestamp": first["timestamp"],
            "last_timestamp": last["timestamp"],
            "first_unix_time": first["unix_time"],
            "last_unix_time": last["unix_time"],
            "entries": self.separator.join(entries),
        }
        template = self.source if last["locale"] == SOURCE_LOCALE else self.target
        return _render_embed(
            template,
            _normalize_mapping(scope, f"{label} values"),
            label,
        )


def _read_yaml(path: Path, label: str) -> dict[str, _YamlValue]:
    try:
        with path.open(encoding="utf-8") as handle:
            payload: object = yaml.load(handle, Loader=_UniqueKeyLoader)
    except (OSError, yaml.YAMLError) as exc:
        raise ValueError(f"Cannot load {label} from {path}: {exc}") from exc
    value = _normalize(payload, label)
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a YAML mapping")
    return value


def _exact_keys(
    value: dict[str, _YamlValue],
    allowed: frozenset[str],
    required: frozenset[str],
    label: str,
) -> None:
    problems: list[str] = []
    if missing := required - value.keys():
        problems.append(f"missing required fields: {sorted(missing)}")
    if unknown := value.keys() - allowed:
        problems.append(f"unknown fields: {sorted(unknown)}")
    if problems:
        raise ValueError(f"{label} is invalid: {'; '.join(problems)}")


def _validate_placeholder_format(
    field_name: str | None,
    format_spec: str,
    label: str,
    scope: _TemplateScope,
) -> None:
    fallback = _FALLBACK_SPEC.fullmatch(format_spec)
    if fallback is not None:
        if field_name not in scope.fallbackable:
            raise ValueError(f"{label} does not support fallback for {field_name!r}")
        return
    truncate = _TRUNCATE_SPEC.fullmatch(format_spec)
    if field_name not in scope.truncatable or truncate is None:
        raise ValueError(
            f"{label} contains an unsupported format specification: {format_spec!r}"
        )
    if int(truncate.group(1)) > _MAX_EMBED_TEXT:
        raise ValueError(f"{label} truncate length must not exceed {_MAX_EMBED_TEXT}")


def _validate_template(
    template: str,
    label: str,
    scope: _TemplateScope,
) -> None:
    try:
        for _, field_name, format_spec, conversion in _FORMATTER.parse(template):
            if field_name is not None and field_name not in scope.placeholders:
                raise ValueError(
                    f"{label} contains an unknown placeholder: {field_name!r}"
                )
            if conversion:
                raise ValueError(f"{label} does not support placeholder conversions")
            if format_spec:
                _validate_placeholder_format(field_name, format_spec, label, scope)
    except ValueError as exc:
        if str(exc).startswith(label):
            raise
        raise ValueError(f"{label} is not a valid template: {exc}") from exc


def _text(
    value: object,
    label: str,
    *,
    rendered: bool,
    limit: int | None = None,
    scope: _TemplateScope = _MESSAGE_SCOPE,
) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be a string")
    if not value:
        raise ValueError(f"{label} must not be empty")
    if rendered:
        if limit is not None and len(value) > limit:
            raise ValueError(f"{label} must not exceed {limit} characters")
    else:
        _validate_template(value, label, scope)
    return value


def _object(value: _YamlValue, label: str) -> dict[str, _YamlValue]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a mapping with string keys")
    return value


def _validate_embed(
    value: _YamlValue,
    label: str,
    *,
    rendered: bool,
    scope: _TemplateScope = _MESSAGE_SCOPE,
) -> dict[str, _YamlValue]:
    embed = _object(value, label)
    _exact_keys(embed, _EMBED_KEYS, frozenset(), label)
    if not embed:
        raise ValueError(f"{label} must contain at least one Discord embed component")

    for key in ("title", "description", "url", "timestamp"):
        if key in embed:
            _text(
                embed[key],
                f"{label}.{key}",
                rendered=rendered,
                limit=_TEXT_LIMITS.get(key),
                scope=scope,
            )

    if "color" in embed:
        color = embed["color"]
        if (
            isinstance(color, bool)
            or not isinstance(color, int)
            or not 0 <= color <= _MAX_COLOR_VALUE
        ):
            raise ValueError(
                f"{label}.color must be an integer from 0 to {_MAX_COLOR_VALUE}"
            )

    for key, (allowed, required) in _OBJECT_SCHEMAS.items():
        if key not in embed:
            continue
        nested = _object(embed[key], f"{label}.{key}")
        _exact_keys(nested, allowed, required, f"{label}.{key}")
        for child, child_value in nested.items():
            _text(
                child_value,
                f"{label}.{key}.{child}",
                rendered=rendered,
                limit=_TEXT_LIMITS.get(f"{key}.{child}"),
                scope=scope,
            )

    if "fields" in embed:
        fields = embed["fields"]
        if (
            not isinstance(fields, list)
            or not fields
            or len(fields) > _MAX_EMBED_FIELDS
        ):
            raise ValueError(
                f"{label}.fields must contain between 1 and {_MAX_EMBED_FIELDS} fields"
            )
        for index, item in enumerate(fields):
            field_label = f"{label}.fields[{index}]"
            field = _object(item, field_label)
            _exact_keys(
                field,
                frozenset({"name", "value", "inline"}),
                frozenset({"name", "value"}),
                field_label,
            )
            _text(
                field["name"],
                f"{field_label}.name",
                rendered=rendered,
                limit=_TEXT_LIMITS["field.name"],
                scope=scope,
            )
            _text(
                field["value"],
                f"{field_label}.value",
                rendered=rendered,
                limit=_TEXT_LIMITS["field.value"],
                scope=scope,
            )
            if "inline" in field and not isinstance(field["inline"], bool):
                raise ValueError(f"{field_label}.inline must be a boolean")
    return embed


def _render_string(template: str, values: Mapping[str, _YamlValue]) -> str:
    scope = {key: "" if value is None else value for key, value in values.items()}
    return _FORMATTER.vformat(template, (), scope)


def _normalize_mapping(
    values: Mapping[str, object],
    label: str,
) -> dict[str, _YamlValue]:
    normalized = _normalize(dict(values), label)
    if not isinstance(normalized, dict):
        raise TypeError("Message values must be a mapping")
    return normalized


def _render_values(
    values: Mapping[str, object],
    label: str,
    diff_styles: _DiffStyles | None,
) -> dict[str, _YamlValue]:
    scope = _normalize_mapping(values, label)
    old = scope.get("old_value")
    new = scope.get("new_value")
    if old is not None and not isinstance(old, str):
        raise ValueError(f"{label}.old_value must be a string or null")
    if new is not None and not isinstance(new, str):
        raise ValueError(f"{label}.new_value must be a string or null")
    key = scope.get("key")
    change_url = scope.get("change_url")
    if key is not None:
        if not isinstance(key, str) or not key:
            raise ValueError(f"{label}.key must be a non-empty string")
        if change_url is not None and not isinstance(change_url, str):
            raise ValueError(f"{label}.change_url must be a string or null")
        scope["key_link"] = f"[`{key}`]({change_url})" if change_url else f"`{key}`"
    if diff_styles is not None:
        old_style, new_style = diff_styles
        old_diff, new_diff = format_ansi_diff(
            old,
            new,
            old_style=old_style,
            new_style=new_style,
        )
        scope["old_diff"] = old_diff
        scope["new_diff"] = new_diff
    return scope


def _render_value(
    value: _YamlValue,
    values: Mapping[str, _YamlValue],
) -> _YamlValue:
    if isinstance(value, str):
        return _render_string(value, values)
    if isinstance(value, list):
        return [_render_value(item, values) for item in value]
    if isinstance(value, dict):
        return {key: _render_value(item, values) for key, item in value.items()}
    return value


def _omit_empty_urls(value: _YamlValue) -> None:
    if not isinstance(value, dict):
        return
    if value.get("url") == "":
        del value["url"]
    author = value.get("author")
    if not isinstance(author, dict):
        return
    for key in ("url", "icon_url"):
        if author.get(key) == "":
            del author[key]


def _render_embed(
    template: dict[str, _YamlValue],
    values: Mapping[str, _YamlValue],
    label: str,
) -> Embed:
    rendered = _render_value(template, values)
    _omit_empty_urls(rendered)
    data = _validate_embed(rendered, label, rendered=True)
    embed = Embed.from_dict(data)
    if "timestamp" in data and embed.timestamp is None:
        raise ValueError(f"{label}.timestamp must be an ISO 8601 timestamp")
    if len(embed) > _MAX_EMBED_TEXT:
        raise ValueError(
            f"{label} must not exceed Discord's "
            f"{_MAX_EMBED_TEXT}-character aggregate limit"
        )
    return embed


def _uses_diff(value: _YamlValue) -> bool:
    if isinstance(value, str):
        return any(
            field_name in _DIFF_PLACEHOLDERS
            for _, field_name, _, _ in _FORMATTER.parse(value)
        )
    if isinstance(value, list):
        return any(_uses_diff(item) for item in value)
    if isinstance(value, dict):
        return any(_uses_diff(item) for item in value.values())
    return False


def _load_diff_styles(
    value: _YamlValue | None,
    label: str,
    *,
    required: bool,
) -> _DiffStyles | None:
    if value is None:
        if required:
            raise ValueError(
                f"{label}.diff is required when diff placeholders are used"
            )
        return None

    raw = _object(value, f"{label}.diff")
    sides = frozenset({"old", "new"})
    _exact_keys(raw, sides, sides, f"{label}.diff")
    styles: list[AnsiStyle] = []
    for side in ("old", "new"):
        style_label = f"{label}.diff.{side}"
        style = _object(raw[side], style_label)
        fields = frozenset({"color", "bold", "underline"})
        _exact_keys(style, fields, fields, style_label)
        color = style["color"]
        bold = style["bold"]
        underline = style["underline"]
        if color is not None and not isinstance(color, str):
            raise ValueError(f"{style_label}.color must be a string or null")
        if not isinstance(bold, bool) or not isinstance(underline, bool):
            raise ValueError(f"{style_label} emphasis settings must be booleans")
        try:
            styles.append(AnsiStyle(color, bold, underline))
        except ValueError as exc:
            raise ValueError(f"{style_label}: {exc}") from exc
    return styles[0], styles[1]


def _load_message(path: Path, name: str) -> MessageTemplate:
    label = f"message {name!r}"
    raw = _read_yaml(path, label)
    actions = frozenset(_ACTIONS)
    _exact_keys(raw, actions | {"diff"}, actions, label)
    templates: dict[Action, dict[str, _YamlValue]] = {}
    for action in _ACTIONS:
        action_label = f"{label} action {action!r}"
        templates[action] = _validate_embed(
            raw[action],
            action_label,
            rendered=False,
        )
    return MessageTemplate(
        name,
        templates,
        _load_diff_styles(
            raw.get("diff"),
            label,
            required=any(_uses_diff(raw[action]) for action in _ACTIONS),
        ),
    )


def _load_merge(path: Path, name: str) -> MergeTemplate:
    label = f"message {name!r} merge"
    raw = _read_yaml(path, label)
    _exact_keys(
        raw,
        frozenset({"source", "target", "diff", "entries"}),
        frozenset({"source", "target", "entries"}),
        label,
    )
    source = _validate_embed(
        raw["source"],
        f"{label}.source",
        rendered=False,
        scope=_MERGE_SCOPE,
    )
    target = _validate_embed(
        raw["target"],
        f"{label}.target",
        rendered=False,
        scope=_MERGE_SCOPE,
    )
    entries = _object(raw["entries"], f"{label}.entries")
    required = frozenset({*_ACTIONS, "separator"})
    _exact_keys(entries, required, required, f"{label}.entries")
    separator = entries["separator"]
    if not isinstance(separator, str):
        raise ValueError(f"{label}.entries.separator must be a string")
    templates: dict[Action, str] = {}
    for action in _ACTIONS:
        templates[action] = _text(
            entries[action],
            f"{label}.entries.{action}",
            rendered=False,
        )
    return MergeTemplate(
        name,
        source,
        target,
        templates,
        separator,
        _load_diff_styles(
            raw.get("diff"),
            label,
            required=any(_uses_diff(template) for template in templates.values()),
        ),
    )


def load_message(name: str, directory: Path) -> MessageTemplate:
    """Load and validate one named detailed-message file."""

    if not _NAME_PATTERN.fullmatch(name):
        raise ValueError(f"Message name must match {_NAME_PATTERN.pattern!r}")
    path = directory / f"{name}.yaml"
    if not path.is_file():
        raise ValueError(f"Unknown message: {name!r}")
    return _load_message(path, name)


def load_merge(name: str, directory: Path) -> MergeTemplate:
    """Load and validate one named merged-message file."""

    if not _NAME_PATTERN.fullmatch(name):
        raise ValueError(f"Message name must match {_NAME_PATTERN.pattern!r}")
    path = directory / f"{name}.yaml"
    if not path.is_file():
        raise ValueError(f"Unknown merge message: {name!r}")
    return _load_merge(path, name)
