from __future__ import annotations

import json

from synthlab.exporters import export_dataset
from synthlab.manifest import (
    build_manifest,
    compute_fingerprint,
    read_manifest,
    table_data_sha256,
    table_digest,
    write_manifest,
)


def _manifest(demo_data, tmp_path, created_at="2025-01-01T00:00:00Z"):
    outputs = export_dataset(
        demo_data, tmp_path, "sqlite,csv,jsonl", blueprint_bytes=demo_data.dataset.blueprint_bytes
    )
    return build_manifest(demo_data, outputs, created_at=created_at), outputs


def test_manifest_contains_required_fields(demo_data, tmp_path):
    manifest, outputs = _manifest(demo_data, tmp_path)
    assert manifest["dataset"] == {
        "name": "demo_shop", "version": "1.0.0", "seed": 20251115,
        "require_boundary_coverage": False,
    }
    assert manifest["blueprint"]["file"] == "blueprint.yaml"
    assert manifest["blueprint"]["sha256"] == demo_data.dataset.blueprint_sha256
    assert manifest["created_at"] == "2025-01-01T00:00:00Z"
    assert manifest["formats"] == ["csv", "jsonl", "sqlite"]
    assert len(manifest["tables"]) == 6
    assert [entry["path"] for entry in manifest["outputs"]] == sorted(item.path for item in outputs)
    assert all(entry["data_sha256"] for entry in manifest["tables"])
    assert len(manifest["content_fingerprint"]) == 64
    assert manifest["boundary_coverage"]["orders"]["note"]["coverage"] == 1.0


def test_created_at_does_not_affect_fingerprint(demo_data, tmp_path):
    first, _ = _manifest(demo_data, tmp_path / "one", "2025-01-01T00:00:00Z")
    second, _ = _manifest(demo_data, tmp_path / "two", "2030-09-09T09:09:09Z")
    assert first["content_fingerprint"] == second["content_fingerprint"]
    assert first["created_at"] != second["created_at"]


def test_table_data_digest_changes_with_content(demo_data):
    digest = table_data_sha256(demo_data.table("customers"))
    table = demo_data.table("customers")
    original = table.rows[0]
    try:
        changed = list(original)
        changed[0] = "CHANGED"
        table.rows[0] = tuple(changed)
        assert table_data_sha256(table) != digest
    finally:
        table.rows[0] = original


def test_fingerprint_changes_when_an_output_hash_changes(demo_data):
    digests = [table_digest(demo_data.table(name)) for name in demo_data.table_order]
    base = [{"path": "data.sqlite3", "bytes": 10, "sha256": "a" * 64}]
    other = [{"path": "data.sqlite3", "bytes": 10, "sha256": "b" * 64}]
    assert compute_fingerprint(demo_data.dataset, digests, base) != compute_fingerprint(
        demo_data.dataset, digests, other
    )
    assert compute_fingerprint(demo_data.dataset, digests, base) == compute_fingerprint(
        demo_data.dataset, list(reversed(digests)), base
    )


def test_manifest_round_trip_and_newline(demo_data, tmp_path):
    manifest, _ = _manifest(demo_data, tmp_path)
    target = tmp_path / "nested" / "manifest.json"
    write_manifest(target, manifest)
    payload = target.read_bytes()
    assert payload.endswith(b"\n")
    assert b"\r\n" not in payload
    loaded = read_manifest(target)
    assert loaded == json.loads(payload)
    assert loaded["content_fingerprint"] == manifest["content_fingerprint"]
