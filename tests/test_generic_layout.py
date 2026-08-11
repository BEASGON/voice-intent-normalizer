"""Contracts for the private versioned generic-adapter layout."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

import voice_intent_normalizer.paths as paths_module
from voice_intent_normalizer.adapters.generic_contract import (
    CAPSULE_PROTOCOL,
    GENERATION_FORMAT,
    LAYOUT_NAME,
    STATUS_FORMAT,
    CapsuleRef,
    GenerationRef,
    build_manifest,
    canonical_json_bytes,
    manifest_digest,
    status_skill_root,
    validate_manifest,
    validate_status_v5,
)
from voice_intent_normalizer.adapters.generic_layout import (
    JournalTransition,
    OwnershipJournal,
    VersionedArtifact,
    generic_layout_paths,
    ownership_journal_bytes,
    validate_ownership_journal,
)
from voice_intent_normalizer.paths import StatePaths

_DIGEST = "a" * 64
_OTHER_DIGEST = "b" * 64
_GENERATION_ID = f"g-{'d' * 64}-{'c' * 32}"
_PREVIOUS_GENERATION_ID = f"g-{'f' * 64}-{'d' * 32}"
_TRANSACTION_ID = f"t-{'0' * 32}"


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


def _status(skill_root: Path) -> dict[str, object]:
    return {
        "format": 5,
        "layout": "versioned-v1",
        "selected_skill_root": str(skill_root),
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


def _journal_status(skill_root: Path, *, phase: str | None) -> bytes:
    """Return exact V5 status bytes for one journal digest transition."""
    status = _status(skill_root)
    status["transaction"] = (
        None if phase is None else {"id": _TRANSACTION_ID, "phase": phase}
    )
    return canonical_json_bytes(status)


def _journal_references() -> tuple[CapsuleRef, GenerationRef]:
    """Return independently literal, clean-V1 journal references."""
    return (
        CapsuleRef(protocol=1, manifest_digest=_DIGEST, package_hash="e" * 64),
        GenerationRef(
            generation_id=_GENERATION_ID,
            manifest_digest=_DIGEST,
            package_hash="d" * 64,
            package_version="1.2.3",
        ),
    )


def _valid_ownership_journal(tmp_path: Path) -> tuple[bytes, Path, Path]:
    """Build one valid upgrade journal and its trusted direct roots."""
    skill_root = tmp_path / "skills"
    skill_root.mkdir()
    capsule, candidate = _journal_references()
    before = _journal_status(skill_root, phase=None)
    after = _journal_status(skill_root, phase="generation-published")
    return (
        ownership_journal_bytes(
            operation="upgrade",
            transaction_id=_TRANSACTION_ID,
            skill_root=skill_root,
            baseline_status_bytes=before,
            before_status_bytes=before,
            after_status_bytes=after,
            capsule=capsule,
            candidate=candidate,
        ),
        skill_root,
        tmp_path / "state" / "generations",
    )


def _journal_with_selected_root(payload: bytes, selected_root: str) -> bytes:
    """Return canonical journal bytes with only its serialized root replaced."""
    value = json.loads(payload)
    value["selected_skill_root"] = selected_root
    return canonical_json_bytes(value)


def test_ownership_journal_upgrade_round_trip_uses_exact_canonical_bytes(
    tmp_path: Path,
):
    """Catch journals that lose a terminal upgrade baseline or owned roots."""
    skill_root = tmp_path / "skills"
    skill_root.mkdir()
    capsule, candidate = _journal_references()
    before = _journal_status(skill_root, phase=None)
    after = _journal_status(skill_root, phase="generation-published")

    payload = ownership_journal_bytes(
        operation="upgrade",
        transaction_id=_TRANSACTION_ID,
        skill_root=skill_root,
        baseline_status_bytes=before,
        before_status_bytes=before,
        after_status_bytes=after,
        capsule=capsule,
        candidate=candidate,
    )
    journal = validate_ownership_journal(
        payload,
        skill_root=skill_root,
        generations_root=tmp_path / "state" / "generations",
    )

    assert payload == canonical_json_bytes(
        {
            "format": 1,
            "operation": "upgrade",
            "transaction_id": _TRANSACTION_ID,
            "selected_skill_root": str(skill_root),
            "baseline_status_digest": hashlib.sha256(before).hexdigest(),
            "status_transition": {
                "before_digest": hashlib.sha256(before).hexdigest(),
                "after_digest": hashlib.sha256(after).hexdigest(),
            },
            "capsule": {
                "protocol": 1,
                "manifest_digest": _DIGEST,
                "package_hash": "e" * 64,
            },
            "candidate": {
                "generation_id": _GENERATION_ID,
                "manifest_digest": _DIGEST,
                "package_hash": "d" * 64,
                "package_version": "1.2.3",
            },
        }
    )
    assert set(json.loads(payload)) == {
        "baseline_status_digest",
        "candidate",
        "capsule",
        "format",
        "operation",
        "selected_skill_root",
        "status_transition",
        "transaction_id",
    }
    assert journal == OwnershipJournal(
        operation="upgrade",
        transaction_id=_TRANSACTION_ID,
        selected_skill_root=skill_root,
        baseline_status_digest=hashlib.sha256(before).hexdigest(),
        transition=JournalTransition(
            before_digest=hashlib.sha256(before).hexdigest(),
            after_digest=hashlib.sha256(after).hexdigest(),
        ),
        capsule=capsule,
        candidate=candidate,
        staging_root=tmp_path / "state" / "staging" / _GENERATION_ID,
        generation_root=tmp_path / "state" / "generations" / _GENERATION_ID,
    )


def test_ownership_journal_first_install_retains_null_initial_baseline(tmp_path: Path):
    """Catch a first-install journal that invents a status baseline."""
    skill_root = tmp_path / "skills"
    skill_root.mkdir()
    capsule, candidate = _journal_references()
    after = _journal_status(skill_root, phase="generation-published")

    journal = validate_ownership_journal(
        ownership_journal_bytes(
            operation="first-install",
            transaction_id=_TRANSACTION_ID,
            skill_root=skill_root,
            baseline_status_bytes=None,
            before_status_bytes=None,
            after_status_bytes=after,
            capsule=capsule,
            candidate=candidate,
        ),
        skill_root=skill_root,
        generations_root=tmp_path / "state" / "generations",
    )

    assert journal.baseline_status_digest is None
    assert journal.transition.before_digest is None


def test_ownership_journal_builder_accepts_prepared_artifacts(tmp_path: Path):
    """Catch installer-prepared artifacts being rejected despite matching references."""
    skill_root = tmp_path / "skills"
    skill_root.mkdir()
    capsule, candidate = _journal_references()
    after = _journal_status(skill_root, phase="generation-published")
    capsule_artifact = VersionedArtifact(
        kind="capsule",
        identifier="voice-intent-normalizer",
        package_version="1",
        package_hash=capsule.package_hash,
        manifest_digest=capsule.manifest_digest,
        manifest_name="capsule.json",
        files={},
    )
    candidate_artifact = VersionedArtifact(
        kind="generation",
        identifier=candidate.generation_id,
        package_version=candidate.package_version,
        package_hash=candidate.package_hash,
        manifest_digest=candidate.manifest_digest,
        manifest_name="generation.json",
        files={},
    )

    journal = validate_ownership_journal(
        ownership_journal_bytes(
            operation="first-install",
            transaction_id=_TRANSACTION_ID,
            skill_root=skill_root,
            baseline_status_bytes=None,
            before_status_bytes=None,
            after_status_bytes=after,
            capsule=capsule_artifact,
            candidate=candidate_artifact,
        ),
        skill_root=skill_root,
        generations_root=tmp_path / "state" / "generations",
    )

    assert (journal.capsule, journal.candidate) == (capsule, candidate)


@pytest.mark.parametrize(
    "mutate",
    (
        lambda value: b'{"format":1,"format":1}',
        lambda value: {**value, "unexpected": True},
        lambda value: {**value, "format": 0},
        lambda value: {**value, "format": 2},
        lambda value: {**value, "operation": "remove"},
        lambda value: {**value, "selected_skill_root": "relative/root"},
        lambda value: {**value, "selected_skill_root": "/different/root"},
        lambda value: {**value, "transaction_id": "t-" + "A" * 32},
        lambda value: {
            **value,
            "candidate": {**value["candidate"], "generation_id": "g-invalid"},
        },
        lambda value: {**value, "capsule": {**value["capsule"], "protocol": 2}},
        lambda value: {
            **value,
            "capsule": {**value["capsule"], "manifest_digest": "A" * 64},
        },
        lambda value: {
            **value,
            "operation": "first-install",
            "baseline_status_digest": _DIGEST,
        },
        lambda value: {**value, "operation": "upgrade", "baseline_status_digest": None},
        lambda value: {
            **value,
            "status_transition": {
                **value["status_transition"],
                "after_digest": value["status_transition"]["before_digest"],
            },
        },
    ),
)
def test_validate_ownership_journal_rejects_untrusted_contract_values(
    tmp_path: Path, mutate
):
    """Catch marker migration, aliases, and values that could widen ownership."""
    skill_root = tmp_path / "skills"
    skill_root.mkdir()
    capsule, candidate = _journal_references()
    before = _journal_status(skill_root, phase=None)
    after = _journal_status(skill_root, phase="generation-published")
    payload = ownership_journal_bytes(
        operation="upgrade",
        transaction_id=_TRANSACTION_ID,
        skill_root=skill_root,
        baseline_status_bytes=before,
        before_status_bytes=before,
        after_status_bytes=after,
        capsule=capsule,
        candidate=candidate,
    )
    value = json.loads(payload)
    altered = mutate(value)
    if isinstance(altered, dict):
        altered = canonical_json_bytes(altered)

    with pytest.raises(ValueError):
        validate_ownership_journal(
            altered,
            skill_root=skill_root,
            generations_root=tmp_path / "state" / "generations",
        )


def test_validate_ownership_journal_rejects_oversized_and_noncanonical_bytes(
    tmp_path: Path,
):
    """Catch parser exhaustion or semantically valid JSON with another spelling."""
    skill_root = tmp_path / "skills"
    skill_root.mkdir()
    capsule, candidate = _journal_references()
    before = _journal_status(skill_root, phase=None)
    after = _journal_status(skill_root, phase="generation-published")
    payload = ownership_journal_bytes(
        operation="upgrade",
        transaction_id=_TRANSACTION_ID,
        skill_root=skill_root,
        baseline_status_bytes=before,
        before_status_bytes=before,
        after_status_bytes=after,
        capsule=capsule,
        candidate=candidate,
    )

    for altered in (payload + b"\n", b" " * (8 * 1024 * 1024 + 1)):
        with pytest.raises(ValueError):
            validate_ownership_journal(
                altered,
                skill_root=skill_root,
                generations_root=tmp_path / "state" / "generations",
            )


def test_validate_ownership_journal_rejects_selected_and_generation_symlinks(
    tmp_path: Path,
):
    """Catch either owned root following a filesystem alias after journal parse."""
    payload, skill_root, generations_root = _valid_ownership_journal(tmp_path)
    generation_target = tmp_path / "generation-target"
    generation_target.mkdir()
    skill_alias = tmp_path / "skill-alias"
    generations_alias = tmp_path / "generations-alias"
    try:
        skill_alias.symlink_to(skill_root, target_is_directory=True)
        generations_alias.symlink_to(generation_target, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"directory symlinks unavailable: {exc}")

    with pytest.raises(ValueError):
        validate_ownership_journal(
            payload, skill_root=skill_alias, generations_root=generations_root
        )
    with pytest.raises(ValueError):
        validate_ownership_journal(
            payload, skill_root=skill_root, generations_root=generations_alias
        )


@pytest.mark.skipif(os.name != "nt", reason="Windows direct-root contract")
def test_validate_ownership_journal_accepts_case_equivalent_windows_roots(
    tmp_path: Path,
):
    """Catch journal trust treating an equivalent Windows root as a mismatch."""
    payload, skill_root, generations_root = _valid_ownership_journal(tmp_path)

    journal = validate_ownership_journal(
        payload,
        skill_root=Path(str(skill_root).swapcase()),
        generations_root=Path(str(generations_root).swapcase()),
    )

    assert journal.selected_skill_root == skill_root
    assert journal.generation_root == generations_root / _GENERATION_ID


@pytest.mark.skipif(os.name != "nt", reason="Windows local-path contract")
@pytest.mark.parametrize(
    "unsafe_root",
    (
        r"\\server\share\generations",
        r"\\.\C:\generations",
        r"C:\generations:journal",
    ),
)
def test_validate_ownership_journal_rejects_windows_network_device_and_ads_roots(
    tmp_path: Path, unsafe_root: str
):
    """Catch journal ownership escaping to UNC, devices, or an ADS."""
    payload, skill_root, _ = _valid_ownership_journal(tmp_path)

    with pytest.raises(ValueError):
        validate_ownership_journal(
            payload, skill_root=skill_root, generations_root=Path(unsafe_root)
        )
    with pytest.raises(ValueError):
        validate_ownership_journal(
            _journal_with_selected_root(payload, unsafe_root),
            skill_root=skill_root,
            generations_root=tmp_path / "state" / "generations",
        )


@pytest.mark.skipif(os.name != "nt", reason="Windows drive classification")
def test_validate_ownership_journal_rejects_mapped_drive_roots(
    tmp_path: Path, monkeypatch
):
    """Catch journal validation accepting a drive reclassified as mapped."""
    payload, skill_root, generations_root = _valid_ownership_journal(tmp_path)
    monkeypatch.setattr(
        paths_module, "_windows_drive_type", lambda _root: 4, raising=False
    )

    with pytest.raises(ValueError):
        validate_ownership_journal(
            payload, skill_root=skill_root, generations_root=generations_root
        )


@pytest.mark.skipif(os.name != "nt", reason="Windows canonical handle paths")
def test_validate_ownership_journal_rejects_windows_reparse_or_junction_roots(
    tmp_path: Path, monkeypatch
):
    """Catch a junction, reparse point, short name, or SUBST alias in a journal root."""
    payload, skill_root, generations_root = _valid_ownership_journal(tmp_path)
    monkeypatch.setattr(
        paths_module,
        "_windows_final_path",
        lambda _path: tmp_path / "different-root",
        raising=False,
    )

    with pytest.raises(ValueError):
        validate_ownership_journal(
            payload, skill_root=skill_root, generations_root=generations_root
        )


@pytest.mark.skipif(os.name == "nt", reason="POSIX direct-root contract")
def test_validate_ownership_journal_rejects_posix_double_slash_aliases(
    tmp_path: Path,
):
    """Catch implementation-defined POSIX aliases becoming owned roots."""
    payload, skill_root, _ = _valid_ownership_journal(tmp_path)
    alias = "//tmp/voice-intent-journal"

    with pytest.raises(ValueError):
        validate_ownership_journal(
            payload, skill_root=skill_root, generations_root=Path(alias)
        )
    with pytest.raises(ValueError):
        validate_ownership_journal(
            _journal_with_selected_root(payload, alias),
            skill_root=skill_root,
            generations_root=tmp_path / "state" / "generations",
        )


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


def test_generic_status_path_uses_private_v1_layout_after_installer_migration(
    tmp_path: Path,
):
    """Catch the migrated installer writing anywhere but its fixed V1 path."""
    state = StatePaths(tmp_path / "state")

    assert state.generic_adapter_root() == state.root / "adapters" / "generic"
    assert state.adapter_status_file("generic") == (
        state.root / "adapters" / "generic" / "status.json"
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
    status = _status(tmp_path / "skills")
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


def test_validate_manifest_bounds_a_deceptive_nested_file_sequence():
    """Catch trusting a nested sequence's false length before iteration."""
    class DeceptiveFiles(list[str]):
        def __init__(self) -> None:
            super().__init__()
            self.iterations = 0

        def __len__(self) -> int:
            return 0

        def __iter__(self):
            for index in range(4097):
                self.iterations += 1
                yield f"file-{index:04d}"

    files = DeceptiveFiles()
    manifest = _manifest()
    manifest["files"] = files
    manifest["file_hashes"] = {}

    with pytest.raises(ValueError):
        validate_manifest(manifest)

    assert files.iterations <= 4097


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
        _status(skill_root), skill_root=skill_root, generations_root=generations
    )

    assert status.capsule_root == skill_root / "voice-intent-normalizer"
    assert status.active_root == generations / _GENERATION_ID
    assert status.previous_root == generations / _PREVIOUS_GENERATION_ID


def test_validate_status_v5_requires_matching_selected_skill_root(tmp_path: Path):
    """Catch protected status omitting or redirecting its durable skill root."""
    skill_root = tmp_path / "skills"
    payload = _status(skill_root)

    status = validate_status_v5(
        payload,
        skill_root=skill_root,
        generations_root=tmp_path / "state" / "generations",
    )

    assert status.selected_skill_root == skill_root


def test_validate_status_v5_rejects_a_mismatched_selected_skill_root(
    tmp_path: Path,
):
    """Catch full status validation trusting a selector over its retained root."""
    trusted = tmp_path / "skills"
    redirected = tmp_path / "other-skills"

    with pytest.raises(ValueError):
        validate_status_v5(
            _status(redirected),
            skill_root=trusted,
            generations_root=tmp_path / "state" / "generations",
        )


@pytest.mark.parametrize(
    ("field", "value"), (("format", 6), ("layout", "versioned-v2"))
)
def test_status_skill_root_rejects_unsupported_status_selector(
    tmp_path: Path, field: str, value: object
):
    """Catch future status selecting a filesystem root before full validation."""
    payload = _status(tmp_path / "skills")
    payload[field] = value

    with pytest.raises(ValueError):
        status_skill_root(canonical_json_bytes(payload))


@pytest.mark.parametrize(
    "mutate",
    (
        lambda status: status.pop("selected_skill_root"),
        lambda status: status.__setitem__("selected_skill_root", "relative/root"),
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
    skill_root = tmp_path / "skills"
    status = _status(skill_root)
    mutate(status)

    with pytest.raises(ValueError):
        validate_status_v5(
            status,
            skill_root=skill_root,
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
