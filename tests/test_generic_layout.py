"""Contracts for the private versioned generic-adapter layout."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from voice_intent_normalizer.adapters.generic_contract import (
    CAPSULE_PROTOCOL,
    GENERATION_FORMAT,
    LAYOUT_NAME,
    STATUS_FORMAT,
    build_manifest,
    canonical_json_bytes,
    manifest_digest,
    validate_manifest,
    validate_status_v5,
)
from voice_intent_normalizer.adapters.generic_layout import generic_layout_paths
from voice_intent_normalizer.paths import StatePaths

_DIGEST = "a" * 64
_OTHER_DIGEST = "b" * 64
_GENERATION_ID = f"g-{'d' * 64}-{'c' * 32}"
_PREVIOUS_GENERATION_ID = f"g-{'f' * 64}-{'d' * 32}"


def _manifest() -> dict[str, object]:
    return {
        "format": 1,
        "kind": "generation",
        "identifier": _GENERATION_ID,
        "package_version": "1.2.3",
        "package_hash": "d" * 64,
        "files": ["SKILL.md", "src/voice_intent_normalizer/cli.py"],
        "file_hashes": {
            "SKILL.md": "1" * 64,
            "src/voice_intent_normalizer/cli.py": "2" * 64,
        },
    }


def _status() -> dict[str, object]:
    return {
        "format": 5,
        "layout": "versioned-v1",
        "capability": "manual",
        "capsule": {
            "protocol": 1,
            "manifest_digest": _DIGEST,
            "package_hash": "e" * 64,
        },
        "active": {
            "generation_id": _GENERATION_ID,
            "manifest_digest": _DIGEST,
            "package_hash": "d" * 64,
            "package_version": "1.2.3",
        },
        "previous": {
            "generation_id": _PREVIOUS_GENERATION_ID,
            "manifest_digest": _OTHER_DIGEST,
            "package_hash": "f" * 64,
            "package_version": "1.2.2",
        },
        "transaction": {"id": f"t-{'0' * 32}", "phase": "activation-pending"},
    }


def test_generic_layout_paths_are_private_and_read_only(tmp_path: Path):
    """Catch layout resolution creating state or using shared adapter names."""
    state = StatePaths(tmp_path / "state")

    layout = generic_layout_paths(state)

    assert layout.adapter_root == state.root / "adapters" / "generic"
    assert layout.status == layout.adapter_root / "status.json"
    assert layout.transaction == layout.adapter_root / "transaction.json"
    assert layout.generations == layout.adapter_root / "generations"
    assert layout.staging == layout.adapter_root / "staging"
    assert layout.retired == layout.adapter_root / "retired"
    assert not state.root.exists()


def test_generic_status_path_stays_legacy_until_installer_migration(tmp_path: Path):
    """Catch Task 1 moving the public path before generic's writer migrates."""
    state = StatePaths(tmp_path / "state")

    assert state.generic_adapter_root() == state.root / "adapters" / "generic"
    assert state.adapter_status_file("generic") == (
        state.root / "adapters" / "generic.json"
    )
    assert state.adapter_status_file("codex") == state.root / "adapters" / "codex.json"
    assert not state.root.exists()


def test_build_manifest_canonicalizes_a_literal_file_mapping():
    """Catch aggregate hashes that depend on mapping insertion order or raw bytes."""
    manifest = build_manifest(
        "generation",
        _GENERATION_ID,
        "1.2.3",
        {"z.txt": b"z", "SKILL.md": b"skill"},
    )

    expected_hashes = {
        "SKILL.md": hashlib.sha256(b"skill").hexdigest(),
        "z.txt": hashlib.sha256(b"z").hexdigest(),
    }
    expected_package_hash = hashlib.sha256(
        b'{"file_hashes":{"SKILL.md":"'
        + expected_hashes["SKILL.md"].encode("ascii")
        + b'","z.txt":"'
        + expected_hashes["z.txt"].encode("ascii")
        + b'"},"files":["SKILL.md","z.txt"],"kind":"generation",'
        + b'"package_version":"1.2.3"}'
    ).hexdigest()

    assert manifest == {
        "format": 1,
        "kind": "generation",
        "identifier": _GENERATION_ID,
        "package_version": "1.2.3",
        "package_hash": expected_package_hash,
        "files": ("SKILL.md", "z.txt"),
        "file_hashes": expected_hashes,
    }
    assert canonical_json_bytes(manifest) == (
        b'{"file_hashes":{"SKILL.md":"'
        + expected_hashes["SKILL.md"].encode("ascii")
        + b'","z.txt":"'
        + expected_hashes["z.txt"].encode("ascii")
        + b'"},"files":["SKILL.md","z.txt"],"format":1,"identifier":"'
        + _GENERATION_ID.encode("ascii")
        + b'","kind":"generation","package_hash":"'
        + expected_package_hash.encode("ascii")
        + b'","package_version":"1.2.3"}'
    )


def test_manifest_round_trip_anchors_status_without_a_hash_fixed_point(
    tmp_path: Path,
):
    """Catch package hashes that include the generation ID they must generate."""
    nonce = "c" * 32
    provisional = build_manifest(
        "generation",
        f"g-{'0' * 64}-{nonce}",
        "1.2.3",
        {"SKILL.md": b"skill"},
    )
    generation_id = f"g-{provisional['package_hash']}-{nonce}"
    manifest = build_manifest(
        "generation", generation_id, "1.2.3", {"SKILL.md": b"skill"}
    )
    status = _status()
    status["active"] = {
        "generation_id": generation_id,
        "manifest_digest": manifest_digest(manifest),
        "package_hash": manifest["package_hash"],
        "package_version": "1.2.3",
    }
    status["previous"] = None

    assert manifest["package_hash"] == provisional["package_hash"]
    assert validate_manifest(manifest) == manifest
    assert validate_status_v5(
        status,
        skill_root=tmp_path / "skills",
        generations_root=tmp_path / "state" / "generations",
    ).active.generation_id == generation_id


@pytest.mark.parametrize(
    "path", ("CON", "dir/NUL.txt", "aux.md", "COM1.py", "x. ", "x ")
)
def test_validate_manifest_rejects_windows_component_aliases(path: str):
    """Catch a POSIX-looking path that Windows aliases to a device or sibling."""
    manifest = _manifest()
    manifest["files"] = [path]
    manifest["file_hashes"] = {path: "1" * 64}

    with pytest.raises(ValueError):
        validate_manifest(manifest)


@pytest.mark.parametrize(
    ("path", "allowed"),
    (
        ("a" * 512, True),
        ("a" * 513, False),
        ("/".join("a" for _ in range(32)), True),
        ("/".join("a" for _ in range(33)), False),
    ),
)
def test_build_manifest_enforces_literal_path_boundaries(path: str, allowed: bool):
    """Catch off-by-one path-length or depth checks in manifest construction."""
    files = {path: b"x"}

    if allowed:
        assert build_manifest(
            "generation", _GENERATION_ID, "1.2.3", files
        )["files"] == (
            path,
        )
    else:
        with pytest.raises(ValueError):
            build_manifest("generation", _GENERATION_ID, "1.2.3", files)


def test_build_manifest_enforces_literal_file_count_boundaries():
    """Catch accepting a generation that exceeds the bounded file table."""
    maximum = {f"file-{index:04d}": b"x" for index in range(4096)}
    oversized = {**maximum, "file-4096": b"x"}

    assert len(
        build_manifest("generation", _GENERATION_ID, "1.2.3", maximum)["files"]
    ) == 4096
    with pytest.raises(ValueError):
        build_manifest("generation", _GENERATION_ID, "1.2.3", oversized)


def test_build_manifest_rejects_an_oversized_mapping_before_iteration():
    """Catch materializing unbounded mapping keys before applying the file limit."""
    class OversizedFiles(dict[str, bytes]):
        def __len__(self) -> int:
            return 4097

        def __iter__(self):
            raise AssertionError("oversized mapping was iterated")

    with pytest.raises(ValueError):
        build_manifest("generation", _GENERATION_ID, "1.2.3", OversizedFiles())


def test_validate_manifest_rejects_oversized_raw_json_before_parsing():
    """Catch feeding an unbounded manifest buffer into the JSON parser."""
    class OversizedBytes(bytes):
        def decode(self, *args, **kwargs):
            raise AssertionError("oversized JSON was parsed")

    with pytest.raises(ValueError):
        validate_manifest(OversizedBytes(b" " * (9 * 1024 * 1024)))


@pytest.mark.parametrize(
    "field, value",
    (
        ("files", ["SKILL.md", "SKILL.md"]),
        ("files", ["../escape.py"]),
        ("files", [r"C:\\escape.py"]),
        ("files", [r"\\server\\share"]),
        ("files", ["name:stream"]),
        ("identifier", "g-" + "A" * 64 + "-" + "c" * 32),
        ("identifier", "g-" + "a" * 64 + "-" + "c" * 31),
        ("format", 2),
        ("package_hash", "A" * 64),
    ),
)
def test_validate_manifest_rejects_untrusted_aliases_and_future_values(
    field: str, value: object
):
    """Catch paths, identifiers, or formats that cannot anchor a generation."""
    manifest = _manifest()
    manifest[field] = value
    if field == "files":
        manifest["file_hashes"] = {str(path): "1" * 64 for path in value}

    with pytest.raises(ValueError):
        validate_manifest(manifest)


def test_validate_manifest_rejects_duplicate_json_keys_and_bad_aggregate_hash():
    """Catch parser ambiguity or a manifest whose file table was substituted."""
    duplicate_key = (
        b'{"format":1,"format":1,"kind":"generation","identifier":"'
        + _GENERATION_ID.encode("ascii")
        + b'","package_version":"1.2.3","package_hash":"'
        + b"d" * 64
        + b'","files":[],"file_hashes":{}}'
    )
    manifest = _manifest()
    manifest["package_hash"] = "0" * 64

    with pytest.raises(ValueError):
        validate_manifest(duplicate_key)
    with pytest.raises(ValueError):
        validate_manifest(manifest)


def test_validate_status_v5_reconstructs_roots_from_trusted_skill_root(tmp_path: Path):
    """Catch status JSON redirecting the bootstrap to an arbitrary runtime path."""
    skill_root = tmp_path / "skills"

    generations = tmp_path / "state" / "adapters" / "generic" / "generations"
    status = validate_status_v5(
        _status(), skill_root=skill_root, generations_root=generations
    )

    assert status.capsule_root == skill_root / "voice-intent-normalizer"
    assert status.active_root == generations / _GENERATION_ID
    assert status.previous_root == generations / _PREVIOUS_GENERATION_ID


@pytest.mark.parametrize(
    "mutate",
    (
        lambda status: status.__setitem__("unexpected", True),
        lambda status: status.__setitem__("format", 6),
        lambda status: status["active"].__setitem__("generation_id", "../escape"),
        lambda status: status["previous"].__setitem__("generation_id", _GENERATION_ID),
        lambda status: status["transaction"].__setitem__("id", "t-" + "A" * 32),
    ),
)
def test_validate_status_v5_rejects_unanchored_or_future_references(
    tmp_path: Path, mutate
):
    """Catch a future, aliased, or duplicate generation becoming selectable."""
    status = _status()
    mutate(status)

    with pytest.raises(ValueError):
        validate_status_v5(
            status,
            skill_root=tmp_path / "skills",
            generations_root=tmp_path / "state" / "generations",
        )


def test_protocol_constants_remain_the_clean_v1_values():
    """Catch a status or capsule format drift selecting an incompatible layout."""
    assert (CAPSULE_PROTOCOL, GENERATION_FORMAT, STATUS_FORMAT, LAYOUT_NAME) == (
        1,
        1,
        5,
        "versioned-v1",
    )
