"""Read-only local preview server. It never binds beyond 127.0.0.1."""

from __future__ import annotations

import html
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote, unquote, urlparse

from .manifest import read_manifest
from .readers import OutputReadError, read_output_tables, safe_output_path
from .spec.loader import load_dataset

STYLE = """
body{font-family:system-ui,-apple-system,Segoe UI,sans-serif;margin:0;background:#f5f2ea;color:#20231f}
header{background:#1e352c;color:#f8f4e8;padding:22px 32px}header h1{margin:0;font-size:24px}
main{max-width:1120px;margin:0 auto;padding:28px}.muted{color:#687166}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(250px,1fr));gap:16px}
.card{background:#fffdf7;border:1px solid #d8d3c3;border-radius:10px;padding:16px}
.card h2{margin:0 0 8px;font-size:17px}a{color:#175c43;text-decoration:none}a:hover{text-decoration:underline}
code,.mono{font-family:ui-monospace,SFMono-Regular,Consolas,monospace;font-size:12px}
table{border-collapse:collapse;width:100%;background:#fffdf7;font-size:12px}
th,td{border:1px solid #ddd7c7;padding:6px 8px;text-align:left;vertical-align:top;max-width:340px;overflow-wrap:anywhere}
th{background:#e9e4d5;position:sticky;top:0}.fail{color:#9c2d20;font-weight:600}.pass{color:#176547;font-weight:600}
pre{white-space:pre-wrap;background:#fffdf7;border:1px solid #d8d3c3;padding:14px;border-radius:8px}
"""


def _page(title: str, body: str) -> bytes:
    return (
        "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
        f"<title>{html.escape(title)}</title><style>{STYLE}</style></head>"
        f"<body><header><h1>synthlab / 造数台</h1></header><main>{body}</main></body></html>"
    ).encode("utf-8")


def _escaped(value: object) -> str:
    return html.escape("" if value is None else str(value))


class PreviewHandler(BaseHTTPRequestHandler):
    out_dir: Path

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        print(f"preview: {self.address_string()} {format % args}")

    def _send(self, status: int, payload: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(payload)

    def _error(self, status: int, message: str) -> None:
        body = f"<h2>{status}</h2><p>{_escaped(message)}</p><p><a href=\"/\">Back</a></p>"
        self._send(status, _page("synthlab error", body), "text/html; charset=utf-8")

    def _drain(self) -> None:
        try:
            remaining = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            remaining = 0
        while remaining > 0:
            chunk = self.rfile.read(min(remaining, 65536))
            if not chunk:
                break
            remaining -= len(chunk)

    def do_HEAD(self) -> None:  # noqa: N802
        self.do_GET()

    def do_POST(self) -> None:  # noqa: N802
        self._drain()
        self._error(405, "This preview is read-only. Only GET and HEAD are supported.")

    do_PUT = do_POST
    do_PATCH = do_POST
    do_DELETE = do_POST

    def _manifest(self) -> dict:
        return read_manifest(self.out_dir / "manifest.json")

    def _raw_paths(self, manifest: dict) -> dict[str, str]:
        return {
            str(record["path"]): str(record.get("format", ""))
            for record in manifest.get("outputs", [])
            if isinstance(record, dict) and isinstance(record.get("path"), str)
        }

    def _index(self) -> None:
        manifest = self._manifest()
        dataset = manifest.get("dataset", {})
        tables = manifest.get("tables", [])
        verification = "not run"
        verification_path = self.out_dir / "verification.json"
        if verification_path.is_file():
            try:
                verification = json.loads(verification_path.read_text(encoding="utf-8")).get("status", "unknown")
            except json.JSONDecodeError:
                verification = "unreadable"
        cards = []
        for table in tables:
            name = str(table.get("name"))
            link = f"/table/{quote(name)}"
            cards.append(
                "<article class=\"card\">"
                f"<h2><a href=\"{link}\">{_escaped(name)}</a></h2>"
                f"<p>{_escaped(table.get('rows'))} rows · pk <code>{_escaped(table.get('primary_key'))}</code></p>"
                f"<p class=\"mono\">sha256 {_escaped(str(table.get('data_sha256', ''))[:16])}…</p></article>"
            )
        body = (
            "<h2>Dataset overview</h2>"
            f"<p><strong>{_escaped(dataset.get('name'))}</strong> version {_escaped(dataset.get('version'))} · "
            f"seed <code>{_escaped(dataset.get('seed'))}</code> · verification "
            f"<span class=\"{'pass' if verification == 'pass' else 'fail'}\">{_escaped(verification)}</span></p>"
            f"<p class=\"mono\">content_fingerprint: {_escaped(manifest.get('content_fingerprint'))}</p>"
            f"<p><a href=\"/manifest.json\">manifest.json</a> · "
            f"<a href=\"/verification.json\">verification.json</a> · "
            f"<a href=\"/verification.md\">verification.md</a></p>"
            "<h2>Tables</h2><section class=\"grid\">" + "".join(cards) + "</section>"
            "<p class=\"muted\">This server is bound to 127.0.0.1 and serves exported files read-only.</p>"
        )
        self._send(200, _page("synthlab preview", body), "text/html; charset=utf-8")

    def _table_page(self, name: str) -> None:
        manifest = self._manifest()
        blueprint = manifest.get("blueprint", {}).get("file")
        if not isinstance(blueprint, str):
            self._error(404, "manifest has no blueprint copy")
            return
        try:
            dataset = load_dataset(safe_output_path(self.out_dir, blueprint))
            tables, errors = read_output_tables(self.out_dir, manifest, dataset)
        except Exception as exc:  # preview must stay available and report the problem as HTML
            self._error(500, f"cannot load preview data: {exc}")
            return
        if name in errors:
            self._error(500, errors[name])
            return
        table = dataset.table(name)
        data = tables.get(name)
        if table is None or data is None:
            self._error(404, f"table {name!r} not found")
            return
        headers = "".join(
            f"<th>{_escaped(column.name)}<br><span class=\"muted\">{_escaped(column.type)}</span></th>"
            for column in table.columns
        )
        rows = "".join(
            "<tr>" + "".join(f"<td>{_escaped(value)}</td>" for value in row) + "</tr>"
            for row in data.rows[:100]
        )
        body = (
            f"<p><a href=\"/\">← All tables</a></p><h2>{_escaped(name)}</h2>"
            f"<p>{data.row_count} rows · showing first {min(100, data.row_count)}</p>"
            f"<div style=\"overflow:auto\"><table><thead><tr>{headers}</tr></thead><tbody>{rows}</tbody></table></div>"
        )
        self._send(200, _page(f"{name} · synthlab", body), "text/html; charset=utf-8")

    def _raw(self, relative: str) -> None:
        manifest = self._manifest()
        if relative not in self._raw_paths(manifest):
            self._error(404, "file is not listed in the manifest")
            return
        try:
            path = safe_output_path(self.out_dir, relative)
            payload = path.read_bytes()
        except (OSError, OutputReadError) as exc:
            self._error(404, str(exc))
            return
        if path.suffix == ".csv":
            content_type = "text/csv; charset=utf-8"
        elif path.suffix == ".jsonl":
            content_type = "application/x-ndjson; charset=utf-8"
        elif path.suffix in (".yaml", ".yml"):
            content_type = "text/yaml; charset=utf-8"
        else:
            content_type = "application/octet-stream"
        self._send(200, payload, content_type)

    def do_GET(self) -> None:  # noqa: N802
        path = unquote(urlparse(self.path).path)
        try:
            if path == "/":
                self._index()
            elif path.startswith("/table/"):
                self._table_page(path[len("/table/"):])
            elif path in ("/manifest.json", "/verification.json", "/verification.md"):
                target = self.out_dir / path.lstrip("/")
                if not target.is_file():
                    self._error(404, f"{path} is not available")
                    return
                content_type = "application/json; charset=utf-8" if target.suffix == ".json" else "text/markdown; charset=utf-8"
                self._send(200, target.read_bytes(), content_type)
            elif path.startswith("/raw/"):
                self._raw(path[len("/raw/"):])
            else:
                self._error(404, "route not found")
        except (OSError, ValueError, KeyError) as exc:
            self._error(500, f"preview error: {exc}")


def create_server(out_dir: str | Path, port: int = 8000) -> ThreadingHTTPServer:
    if not 0 <= port <= 65535:
        raise ValueError("port must be between 0 and 65535")
    target = Path(out_dir).resolve()
    if not (target / "manifest.json").is_file():
        raise FileNotFoundError(f"manifest.json not found in {target}")
    handler = type("BoundPreviewHandler", (PreviewHandler,), {"out_dir": target})
    server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    server.daemon_threads = True
    return server


def serve_preview(out_dir: str | Path, port: int = 8000) -> None:
    server = create_server(out_dir, port)
    host, selected_port = server.server_address[:2]
    print(f"synthlab preview: http://{host}:{selected_port}/ (read-only, Ctrl+C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
