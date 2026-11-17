from __future__ import annotations

from pathlib import Path

import pytest

from synthlab.engine import generate_dataset
from synthlab.exporters import export_dataset
from synthlab.manifest import build_manifest, write_manifest
from synthlab.spec.loader import load_dataset

ROOT = Path(__file__).resolve().parents[1]
DEMO_YAML = ROOT / "fixtures" / "demo" / "dataset.yaml"


@pytest.fixture(scope="session")
def demo_spec():
    return load_dataset(DEMO_YAML)


@pytest.fixture(scope="session")
def demo_data(demo_spec):
    return generate_dataset(demo_spec)


@pytest.fixture(scope="session")
def demo_output(demo_data, tmp_path_factory):
    target = tmp_path_factory.mktemp("demo_output")
    outputs = export_dataset(
        demo_data,
        target,
        ("sqlite", "csv", "jsonl"),
        blueprint_bytes=demo_data.dataset.blueprint_bytes,
    )
    manifest = build_manifest(demo_data, outputs)
    write_manifest(target / "manifest.json", manifest)
    return target


@pytest.fixture
def write_yaml(tmp_path):
    def _write(text: str, name: str = "dataset.yaml") -> Path:
        target = tmp_path / name
        target.write_text(text, encoding="utf-8")
        return target

    return _write
