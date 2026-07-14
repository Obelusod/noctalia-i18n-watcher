"""Source adapter contracts and selection."""

from __future__ import annotations

from collections.abc import Mapping
from importlib import import_module
from typing import Protocol, runtime_checkable

from ..models import (
    PROJECT_ID_PATTERN,
    Change,
    JsonValue,
    PollResult,
    ValidationError,
    WatcherError,
)


@runtime_checkable
class Source(Protocol):
    """Acquisition boundary for normalized Noctalia translation data."""

    def poll(self, cursor: JsonValue | None, /) -> PollResult:
        """Collect normalized changes, a cursor, and any required source texts.

        ``None`` establishes an initial position without replaying history.
        Returned changes are unique and ordered from oldest to newest.
        Initial polls and polls containing changes include the complete current
        source-language mapping (``en`` for Noctalia). An unchanged subsequent
        poll may omit it.
        Cursors from another adapter or project fail instead of silently
        rebaselining.
        Locale identifiers belong to the selected localization project.
        Change and editor URLs are optional metadata.
        """

        ...

    def close(self) -> None:
        """Release sessions, connections, and other owned resources."""

        ...


@runtime_checkable
class PagedSource(Protocol):
    """Optional capability for reading historical change pages."""

    def page(self, number: int, /) -> tuple[Change, ...]:
        """Return one source-native history page using a one-based number."""

        ...


class _SourceFactory(Protocol):
    """Factory exported by each source adapter; construction performs no network I/O."""

    def __call__(
        self,
        options: Mapping[str, JsonValue],
        *,
        project: str,
        timeout: float,
        proxy: str,
    ) -> object: ...


def build_source(
    adapter: str,
    options: Mapping[str, JsonValue],
    *,
    project: str,
    timeout: float,
    proxy: str,
) -> Source:
    """Load ``sources.<adapter>`` for the selected localization project."""

    if not PROJECT_ID_PATTERN.fullmatch(project):
        raise ValidationError("source.project must be a lowercase project identifier")
    module_name = f"{__name__}.{adapter}"
    try:
        module = import_module(module_name)
    except ModuleNotFoundError as exc:
        if exc.name != module_name:
            raise
        raise WatcherError(f"Unsupported source: {adapter!r}") from None
    factory: _SourceFactory | None = getattr(module, "create", None)
    if not callable(factory):
        raise WatcherError(f"Source {adapter!r} has no create() function")
    source = factory(options, project=project, timeout=timeout, proxy=proxy)
    if not isinstance(source, Source):
        raise WatcherError(f"Source {adapter!r} does not implement Source")
    return source
