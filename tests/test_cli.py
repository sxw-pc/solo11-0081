from __future__ import annotations

import http.client
import json
import threading
from pathlib import Path

from synthlab.cli import main
from synthlab.preview import create_server

ROOT = Path(__file__).resolve().parents[1]


def test_demo_runs_and_verifies(tmp_path, capsys):
    target = tmp_path / "demo"
    assert main(["demo", "--out", str(target)]) == 0
    output = capsys.readouterr().out
    assert "content_fingerprint:" in output
    assert "verification: pass" in output
    assert "exit code: 0" in output
    assert (target / "manifest.json").is_file()
    assert (target / "verification.json").is_file()
    assert (target / "verification.md").is_file()


def test_demo_is_repeatable_apart_from_timestamp(tmp_path):
    target = tmp_path / "demo"
    assert main(["demo", "--out", str(target)]) == 0
    first = json.loads((target / "manifest.json").read_text(encoding="utf-8"))
    assert main(["demo", "--out", str(target)]) == 0
    second = json.loads((target / "manifest.json").read_text(encoding="utf-8"))
    assert first["content_fingerprint"] == second["content_fingerprint"]
    first.pop("created_at")
    second.pop("created_at")
    assert first == second


def test_generate_plan_and_verify_commands(tmp_path, capsys):
    target = tmp_path / "generated"
    spec = ROOT / "fixtures" / "demo" / "dataset.yaml"
    assert main(["generate", "--spec", str(spec), "--out", str(target), "--formats", "csv"]) == 0
    assert (target / "csv" / "customers.csv").is_file()
    assert main(["verify", "--out", str(target)]) == 0
    output = capsys.readouterr().out
    assert "verification: pass" in output
    assert main(["plan", "--spec", str(spec)]) == 0
    assert "Execution plan" in capsys.readouterr().out


def test_invalid_blueprint_returns_one_without_traceback(capsys):
    invalid = ROOT / "fixtures" / "invalid" / "cyclic_reference.yaml"
    assert main(["plan", "--spec", str(invalid)]) == 1
    captured = capsys.readouterr()
    assert "SPEC005" in captured.err
    assert "Traceback" not in captured.err


def test_verify_detects_tamper_and_returns_one(tmp_path, capsys):
    target = tmp_path / "demo"
    assert main(["demo", "--out", str(target)]) == 0
    target.joinpath("jsonl", "regions.jsonl").write_text("{}\n", encoding="utf-8")
    assert main(["verify", "--out", str(target)]) == 1
    assert "verification: fail" in capsys.readouterr().out
def test_preview_binds_loopback_and_is_read_only(demo_output):
    server = create_server(demo_output, 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[:2]
    try:
        assert host == "127.0.0.1"
        connection = http.client.HTTPConnection(host, port, timeout=5)
        connection.request("GET", "/")
        response = connection.getresponse()
        body = response.read().decode("utf-8")
        assert response.status == 200
        assert "demo_shop" in body
        assert "http://" not in body
        connection.request("GET", "/table/orders")
        response = connection.getresponse()
        assert response.status == 200
        assert "ORD-" in response.read().decode("utf-8")
        connection.request("GET", "/raw/csv/orders.csv")
        response = connection.getresponse()
        assert response.status == 200
        assert response.read().startswith(b"order_id,")
        connection.request("POST", "/", body=b"x", headers={"Content-Length": "1"})
        response = connection.getresponse()
        assert response.status == 405
        response.read()
        connection.request("GET", "/raw/manifest.json")
        response = connection.getresponse()
        assert response.status == 404
        response.read()
        connection.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
