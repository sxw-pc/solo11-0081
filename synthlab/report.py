"""Render verification payloads as JSON and Markdown."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def _escape(value: Any) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")


def render_markdown(payload: dict[str, Any]) -> str:
    status = str(payload.get("status", "fail")).upper()
    summary = payload.get("summary", {})
    lines = [
        "# Synthlab verification",
        "",
        f"- **Status:** {status}",
        f"- **Dataset:** {payload.get('dataset') or 'unknown'}",
        f"- **Checked at (UTC):** {payload.get('checked_at', '')}",
        f"- **Checks:** {summary.get('passed', 0)} passed, {summary.get('failed', 0)} failed",
        f"- **Content fingerprint:** `{payload.get('content_fingerprint', '')}`",
        "",
        "## Checks",
        "",
        "| Status | Code | Check | Location | Message |",
        "| --- | --- | --- | --- | --- |",
    ]
    for check in payload.get("checks", []):
        lines.append(
            "| {status} | {code} | {id} | {location} | {message} |".format(
                status=_escape(check.get("status", "")),
                code=_escape(check.get("code", "OK")),
                id=_escape(check.get("id", "")),
                location=_escape(check.get("location") or "-"),
                message=_escape(check.get("message", "")),
            )
        )
    boundary = payload.get("boundary_coverage", {})
    if boundary:
        lines.extend([
            "",
            "## Boundary coverage",
            "",
            "| Table | Column | Coverage | Missing |",
            "| --- | --- | --- | --- |",
        ])
        for table, columns in boundary.items():
            for column, result in columns.items():
                lines.append(
                    f"| {_escape(table)} | {_escape(column)} | "
                    f"{result.get('coverage', 0):.0%} | {_escape(result.get('missing', []))} |"
                )
    issues = payload.get("issues", [])
    if issues:
        lines.extend(["", "## Failure details", ""])
        lines.extend(f"- `{issue.get('code')}` {issue.get('message')}" for issue in issues)
    return "\n".join(lines) + "\n"


def write_verification_reports(out_dir: str | Path, payload: dict[str, Any]) -> tuple[Path, Path]:
    target = Path(out_dir)
    target.mkdir(parents=True, exist_ok=True)
    json_path = target / "verification.json"
    markdown_path = target / "verification.md"
    with json_path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False))
        handle.write("\n")
    with markdown_path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(render_markdown(payload))
    return json_path, markdown_path
