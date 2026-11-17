"""Stable, structured errors used by every synthlab layer."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable


@dataclass(slots=True)
class Issue:
    """A single machine-readable problem with enough context to locate it."""

    code: str
    message: str
    path: str | None = None
    table: str | None = None
    column: str | None = None

    def location(self) -> str:
        parts = [part for part in (self.path, self.table, self.column) if part]
        return ":".join(parts)

    def render(self) -> str:
        location = self.location()
        suffix = f" ({location})" if location else ""
        return f"[{self.code}] {self.message}{suffix}"

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "path": self.path,
            "table": self.table,
            "column": self.column,
            "location": self.location() or None,
        }


class SynthlabError(Exception):
    """Base exception whose text and payload are safe to show to a user."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        path: str | None = None,
        table: str | None = None,
        column: str | None = None,
        issues: Iterable[Issue] | None = None,
    ) -> None:
        self.code = code
        self.issues: list[Issue] = list(issues or [])
        if not self.issues:
            self.issues = [Issue(code, message, path, table, column)]
        self.issue = self.issues[0]
        self.message = message
        rendered = "\n".join(issue.render() for issue in self.issues)
        super().__init__(rendered)

    @property
    def path(self) -> str | None:
        return self.issue.path

    @property
    def table(self) -> str | None:
        return self.issue.table

    @property
    def column(self) -> str | None:
        return self.issue.column

    def as_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "issues": [i.as_dict() for i in self.issues]}


class SpecValidationError(SynthlabError):
    """Aggregated blueprint validation failure."""

    def __init__(self, issues: Iterable[Issue]) -> None:
        collected = list(issues)
        if not collected:
            collected = [Issue("SPEC000", "blueprint validation failed")]
        first = collected[0]
        summary = first.message if len(collected) == 1 else f"{len(collected)} blueprint problems; first: {first.message}"
        super().__init__(first.code, summary, issues=collected)


class GenerationError(SynthlabError):
    """A deterministic generation rule could not be satisfied."""


class ExportError(SynthlabError):
    """Writing an output artefact failed."""


def make_issue(
    code: str,
    message: str,
    *,
    path: str | None = None,
    table: str | None = None,
    column: str | None = None,
) -> Issue:
    return Issue(code=code, message=message, path=path, table=table, column=column)


@dataclass(slots=True)
class IssueBag:
    """Small helper for validators that collect several problems before raising."""

    path: str | None = None
    issues: list[Issue] = field(default_factory=list)

    def add(self, code: str, message: str, *, table: str | None = None, column: str | None = None) -> None:
        self.issues.append(Issue(code, message, self.path, table, column))

    def extend(self, issues: Iterable[Issue]) -> None:
        self.issues.extend(issues)
