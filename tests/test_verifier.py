from __future__ import annotations

import json
import shutil

from synthlab.verifier import verify_dataset


def _copy_output(demo_output, tmp_path):
    target = tmp_path / "output"
    shutil.copytree(demo_output, target)
    return target


def _mutate_jsonl(path, row_index: int, column: str, value) -> None:
    lines = path.read_text(encoding="utf-8").splitlines()
    payload = json.loads(lines[row_index])
    payload[column] = value
    lines[row_index] = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")


def _codes(result):
    return {check.code for check in result.checks if check.status == "fail"}


def test_clean_output_passes_and_writes_reports(demo_output):
    result = verify_dataset(demo_output)
    assert result.ok, [check.message for check in result.checks if check.status == "fail"]
    json_path, markdown_path = result.write()
    assert json_path.is_file() and markdown_path.is_file()
    payload = json.loads(json_path.read_text(encoding="utf-8"))
    assert payload["status"] == "pass"
    assert payload["summary"]["failed"] == 0
    assert any(check["id"] == "fingerprint" for check in payload["checks"])
    assert "Status:** PASS" in markdown_path.read_text(encoding="utf-8")
    assert result.content_fingerprint


def test_missing_manifest_is_ver001(tmp_path):
    result = verify_dataset(tmp_path)
    assert not result.ok
    assert "VER001" in _codes(result)


def test_missing_output_file_is_ver001_and_fingerprint_fails(demo_output, tmp_path):
    target = _copy_output(demo_output, tmp_path)
    (target / "jsonl" / "customers.jsonl").unlink()
    result = verify_dataset(target)
    assert "VER001" in _codes(result)
    assert "VER008" in _codes(result)


def test_tampered_file_is_ver002(demo_output, tmp_path):
    target = _copy_output(demo_output, tmp_path)
    with (target / "csv" / "regions.csv").open("ab") as handle:
        handle.write(b"x")
    result = verify_dataset(target)
    assert "VER002" in _codes(result)


def test_tampered_enum_is_caught_as_ver006(demo_output, tmp_path):
    target = _copy_output(demo_output, tmp_path)
    _mutate_jsonl(target / "jsonl" / "customers.jsonl", 0, "status", "bogus")
    result = verify_dataset(target)
    codes = _codes(result)
    assert "VER002" in codes
    assert "VER006" in codes


def test_tampered_primary_key_is_ver004(demo_output, tmp_path):
    target = _copy_output(demo_output, tmp_path)
    path = target / "jsonl" / "customers.jsonl"
    first = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
    _mutate_jsonl(path, 1, "customer_id", first["customer_id"])
    result = verify_dataset(target)
    assert "VER004" in _codes(result)


def test_tampered_foreign_key_is_ver005(demo_output, tmp_path):
    target = _copy_output(demo_output, tmp_path)
    _mutate_jsonl(target / "jsonl" / "order_items.jsonl", 0, "order_id", "ORD-NOT-REAL")
    result = verify_dataset(target)
    assert "VER005" in _codes(result)


def test_tampered_blueprint_is_ver012(demo_output, tmp_path):
    target = _copy_output(demo_output, tmp_path)
    path = target / "blueprint.yaml"
    path.write_text(path.read_text(encoding="utf-8") + "\n# changed\n", encoding="utf-8")
    result = verify_dataset(target)
    assert "VER012" in _codes(result)
