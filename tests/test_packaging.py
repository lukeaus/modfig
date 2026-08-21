from __future__ import annotations

import os
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest

from modfig.resources import read_resource_bytes

ROOT = Path(__file__).resolve().parent.parent
REPRESENTATIVE_ASSETS = (
    "capability-matrix.json",
    "proof-record.schema.json",
    "modfig-registry-0.1.md",
    "modfig-registry-0.1.schema.json",
    "fixtures/valid/factory/oh-my-droid/factory-client-config-v0.1.yaml",
    "fixtures/valid/cursor/cursor-client-config-v0.1.yaml",
    "fixtures/invalid/factory/factory-defaults-missing-role-v0.1.yaml",
    "fixtures/invalid/factory/factory-defaults-native-reference-v0.1.yaml",
    "fixtures/invalid/factory/client-config-reserved-component-v0.1.yaml",
    "fixtures/invalid/factory/client-config-invalid-logical-name-v0.1.yaml",
    "fixtures/invalid/factory/client-config-empty-core-v0.1.yaml",
    "fixtures/invalid/factory/client-config-empty-extension-v0.1.yaml",
    "fixtures/invalid/factory/client-config-nonmapping-core-v0.1.yaml",
    "fixtures/proof/chatgpt/chatgpt-shared-config.contract.json",
)


@pytest.mark.parametrize(
    "path", ["", "/spec/a.json", "spec//a.json", "spec/../a.json", "spec\\a.json"]
)
def test_resource_reader_rejects_unsafe_relative_paths(path: str) -> None:
    with pytest.raises(ValueError, match="unsafe resource path"):
        read_resource_bytes(path)


@pytest.mark.parametrize("relative_path", REPRESENTATIVE_ASSETS)
def test_packaged_spec_assets_are_byte_identical(relative_path: str) -> None:
    assert (
        read_resource_bytes(f"spec/{relative_path}") == (ROOT / "spec" / relative_path).read_bytes()
    )


@pytest.mark.parametrize(
    "relative_path",
    [
        path.relative_to(ROOT / "spec").as_posix()
        for path in sorted((ROOT / "spec" / "fixtures").rglob("*"))
        if path.is_file()
    ],
)
def test_packaged_v01_fixtures_are_byte_identical(relative_path: str) -> None:
    assert (
        read_resource_bytes(f"spec/{relative_path}") == (ROOT / "spec" / relative_path).read_bytes()
    )


def test_v01_spec_assets_are_packaged_byte_identically() -> None:
    assert (
        read_resource_bytes("spec/modfig-registry-0.1.md")
        == (ROOT / "spec/modfig-registry-0.1.md").read_bytes()
    )
    assert (
        read_resource_bytes("spec/capability-matrix.json")
        == (ROOT / "spec/capability-matrix.json").read_bytes()
    )


def test_wheel_and_sdist_contain_required_assets(tmp_path: Path) -> None:
    subprocess.run(
        [sys.executable, "-m", "build", "--outdir", str(tmp_path)],
        cwd=ROOT,
        check=True,
    )
    wheel = next(tmp_path.glob("*.whl"))
    sdist = next(tmp_path.glob("*.tar.gz"))
    required = {f"modfig/resources/spec/{path}" for path in REPRESENTATIVE_ASSETS}

    with zipfile.ZipFile(wheel) as archive:
        assert required <= set(archive.namelist())
    with tarfile.open(sdist) as archive:
        names = {"/".join(name.split("/")[1:]) for name in archive.getnames()}
        assert {f"spec/{path.removeprefix('modfig/resources/spec/')}" for path in required} <= names


def test_installed_wheel_loads_resources_without_source_tree(tmp_path: Path) -> None:
    dist = tmp_path / "dist"
    target = tmp_path / "site"
    cwd = tmp_path / "empty"
    dist.mkdir()
    cwd.mkdir()
    subprocess.run(
        [sys.executable, "-m", "build", "--wheel", "--outdir", str(dist)],
        cwd=ROOT,
        check=True,
    )
    subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--no-deps",
            "--target",
            str(target),
            str(next(dist.glob("*.whl"))),
        ],
        check=True,
    )
    env = {**os.environ, "PYTHONPATH": str(target)}
    subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            (
                "import sys; sys.path.insert(0, sys.argv[1]); "
                "from modfig.proof import load_capability_matrix; "
                "assert load_capability_matrix()['matrixVersion'] == 1"
            ),
            str(target),
        ],
        cwd=cwd,
        env=env,
        check=True,
    )
