from __future__ import annotations

import json
from pathlib import Path

from synthlab.engine import generate_dataset
from synthlab.exporters import export_dataset
from synthlab.manifest import build_manifest, table_data_sha256
from synthlab.spec.loader import load_dataset

ROOT = Path(__file__).resolve().parents[1]
GOLDEN = ROOT / "golden" / "demo_fingerprint.json"


def test_demo_matches_golden_fingerprint(tmp_path):
    expected = json.loads(GOLDEN.read_text(encoding="utf-8"))
    spec = load_dataset(ROOT / "fixtures" / "demo" / "dataset.yaml")
    data = generate_dataset(spec)
    outputs = export_dataset(
        data, tmp_path, ("sqlite", "csv", "jsonl"), blueprint_bytes=spec.blueprint_bytes
    )
    manifest = build_manifest(data, outputs, created_at="2025-01-01T00:00:00Z")
    assert spec.blueprint_sha256 == expected["blueprint_sha256"]
    assert manifest["content_fingerprint"] == expected["content_fingerprint"]
    for name, digest in expected["table_data_sha256"].items():
        assert table_data_sha256(data.table(name)) == digest
