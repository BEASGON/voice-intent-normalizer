"""Stable local command-line surface for voice-intent normalization."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import sys
from collections.abc import Callable, Sequence
from dataclasses import asdict
from importlib import resources
from pathlib import Path
from typing import Any, TextIO

from .adapters.base import AdapterResult, InstallOptions, UninstallOptions
from .adapters.codex import CodexAdapter
from .adapters.generic import GenericAdapter
from .adapters.generic_contract import canonical_json_bytes, validate_manifest
from .adapters.generic_layout import generation_source_files
from .adapters.openclaw import OpenClawAdapter
from .adapters.workbuddy import WorkBuddyAdapter
from .installer import Installer
from .models import Candidate, CorrectionDecision, Scope
from .paths import StatePaths, guard_project_root
from .project_scan import scan_project
from .service import NormalizeRequest, NormalizerService

GENERATION_MANIFEST_FILENAME = "generation.json"
_WINDOWS_REPARSE_POINT = 0x400


class _UsageError(ValueError):
    pass


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise _UsageError(message)


def build_parser() -> argparse.ArgumentParser:
    """Build the documented command surface without performing any I/O."""
    parser = _Parser(prog="voice-intent")
    commands = parser.add_subparsers(dest="command", required=True)
    normalize = commands.add_parser("normalize")
    normalize.add_argument("--text", required=True)
    normalize.add_argument("--domain", action="append", default=[])
    normalize.add_argument("--conversation-term", action="append", default=[])
    normalize.add_argument("--project-root")
    normalize.add_argument("--json", action="store_true")

    learn = commands.add_parser("learn")
    learn.add_argument("--alias", required=True)
    learn.add_argument("--canonical", required=True)
    learn.add_argument("--scope", choices=("personal", "project"), default="personal")
    learn.add_argument("--project-root")
    learn.add_argument("--json", action="store_true")

    reject = commands.add_parser("reject")
    reject.add_argument("--alias", required=True)
    reject.add_argument("--canonical", required=True)
    reject.add_argument("--json", action="store_true")

    undo = commands.add_parser("undo")
    undo.add_argument("--json", action="store_true")
    listing = commands.add_parser("list")
    listing.add_argument("--limit", type=int, default=20)
    listing.add_argument("--json", action="store_true")
    scan = commands.add_parser("scan-project")
    scan.add_argument("--project-root", required=True)
    scan.add_argument("--json", action="store_true")
    update = commands.add_parser("update")
    update.add_argument("--json", action="store_true")
    doctor = commands.add_parser("doctor")
    doctor.add_argument("--json", action="store_true")
    doctor.add_argument("--platform", action="append", default=[])
    doctor.add_argument("--all-detected", action="store_true")
    for name in ("install", "uninstall"):
        command = commands.add_parser(name)
        command.add_argument("--json", action="store_true")
        command.add_argument("--platform", action="append", default=[])
        command.add_argument("--all-detected", action="store_true")
        command.add_argument("--output-dir")
        command.add_argument("--workspace")
        command.add_argument("--strict", action="store_true")
    install = commands.choices["install"]
    install.add_argument(
        "--auto-update", action=argparse.BooleanOptionalAction, default=None
    )
    install.add_argument("--implicit-invocation-confirmed", action="store_true")
    commands.choices["uninstall"].add_argument(
        "--remove-shared-data", action="store_true"
    )
    commands.add_parser("hook")
    return parser


def default_service() -> NormalizerService:
    """Create the local service using packaged lexicons when they are present."""
    repository = _runtime_repository()
    return NormalizerService(StatePaths.resolve(), repository / "assets" / "lexicons")


def default_installer(paths: StatePaths | None = None) -> Installer:
    """Return adapters whose public capabilities this build can verify."""
    repository = _runtime_repository()
    state = StatePaths.resolve() if paths is None else paths
    generic = GenericAdapter(repository, state)
    codex = CodexAdapter(repository, state)
    openclaw = OpenClawAdapter(repository, state)
    workbuddy = WorkBuddyAdapter(repository, state)
    return Installer(
        {
            codex.platform: codex,
            openclaw.platform: openclaw,
            workbuddy.platform: workbuddy,
            generic.platform: generic,
        }
    )


def _runtime_repository() -> Path:
    """Return one exact checkout, wheel bundle, or anchored generation root."""
    module = _physical_regular_file(Path(__file__), "runtime CLI module")
    checkout = module.parents[2]
    checkout_module = checkout / "src" / "voice_intent_normalizer" / "cli.py"
    if _same_physical_file(module, checkout_module):
        manifest_path = checkout / GENERATION_MANIFEST_FILENAME
        if _entry_exists(manifest_path):
            return _validated_generation_repository(checkout, module)
        if _runtime_repository_complete(checkout):
            return checkout.resolve(strict=True)
        raise RuntimeError("runtime checkout is incomplete")

    package_root = module.parent
    expected_bundle = package_root / "_skill_bundle"
    bundle_resource = resources.files("voice_intent_normalizer").joinpath(
        "_skill_bundle"
    )
    try:
        bundle = Path(os.fspath(bundle_resource)).resolve(strict=True)
    except (OSError, TypeError):
        raise RuntimeError("runtime skill bundle is not a physical directory") from None
    if not _same_physical_directory(bundle, expected_bundle):
        raise RuntimeError("runtime skill bundle does not belong to imported package")
    if not _runtime_repository_complete(bundle, exact=True):
        raise RuntimeError("runtime skill bundle is unavailable")
    return bundle


def _validated_generation_repository(repository: Path, module: Path) -> Path:
    source = repository / "src"
    for entry in os.environ.get("PYTHONPATH", "").split(os.pathsep):
        if entry and _same_resolved_path(Path(entry), source):
            raise RuntimeError(
                "runtime generation cannot be selected through PYTHONPATH"
            )
    expected_module = source / "voice_intent_normalizer" / "cli.py"
    if not _same_physical_file(module, expected_module):
        raise RuntimeError("runtime generation module origin does not match")
    try:
        expected = generation_source_files(repository)
        actual = _direct_tree_files(repository)
        manifest_bytes = actual[GENERATION_MANIFEST_FILENAME]
        manifest = validate_manifest(manifest_bytes)
    except (KeyError, OSError, RuntimeError, ValueError) as exc:
        raise RuntimeError("runtime generation is unavailable") from exc
    if canonical_json_bytes(manifest) != manifest_bytes:
        raise RuntimeError("runtime generation manifest is not canonical")
    expected_names = set(expected)
    if (
        set(actual) != {*expected_names, GENERATION_MANIFEST_FILENAME}
        or manifest["kind"] != "generation"
        or manifest["identifier"] != repository.name
        or set(manifest["files"]) != expected_names
    ):
        raise RuntimeError("runtime generation manifest does not match source tree")
    hashes = manifest["file_hashes"]
    if any(
        hashes[relative] != hashlib.sha256(data).hexdigest()
        for relative, data in expected.items()
    ):
        raise RuntimeError("runtime generation file hash does not match")
    return repository.resolve(strict=True)


def _runtime_repository_complete(repository: Path, *, exact: bool = False) -> bool:
    try:
        expected = generation_source_files(repository)
        allowed_bytecode = _allowed_installer_bytecode_paths(expected)
        if (
            exact
            and _direct_tree_files(
                repository, allowed_bytecode=allowed_bytecode
            )
            != expected
        ):
            return False
    except (OSError, RuntimeError, ValueError):
        return False
    return True


def _direct_tree_files(
    root: Path, *, allowed_bytecode: frozenset[str] | None = None
) -> dict[str, bytes]:
    _physical_directory(root, "runtime repository")
    pending = [root]
    files: dict[str, bytes] = {}
    while pending:
        directory = pending.pop()
        try:
            entries = tuple(os.scandir(directory))
        except OSError as exc:
            raise RuntimeError("runtime repository is unavailable") from exc
        for entry in entries:
            path = Path(entry.path)
            try:
                info = path.lstat()
            except OSError as exc:
                raise RuntimeError("runtime repository is unavailable") from exc
            if _is_alias(info):
                raise RuntimeError("runtime repository contains an alias")
            if stat.S_ISDIR(info.st_mode):
                if allowed_bytecode is not None and path.name == "__pycache__":
                    _validate_installer_bytecode_cache(
                        path, root, allowed_bytecode
                    )
                    continue
                pending.append(path)
                continue
            if not stat.S_ISREG(info.st_mode):
                raise RuntimeError("runtime repository contains an alias")
            relative = path.relative_to(root).as_posix()
            files[relative] = path.read_bytes()
    return files


def _allowed_installer_bytecode_paths(
    sources: dict[str, bytes],
) -> frozenset[str]:
    cache_tag = sys.implementation.cache_tag
    if not isinstance(cache_tag, str) or not cache_tag:
        raise RuntimeError("runtime bytecode cache tag is unavailable")
    allowed: set[str] = set()
    for relative in sources:
        source = Path(relative)
        if source.suffix != ".py":
            continue
        prefix = source.parent / "__pycache__" / f"{source.stem}.{cache_tag}"
        allowed.add((prefix.parent / f"{prefix.name}.pyc").as_posix())
        for optimization in (1, 2):
            allowed.add(
                prefix.with_name(
                    f"{prefix.name}.opt-{optimization}.pyc"
                ).as_posix()
            )
    return frozenset(allowed)


def _validate_installer_bytecode_cache(
    directory: Path,
    root: Path,
    allowed: frozenset[str],
) -> None:
    try:
        entries = tuple(os.scandir(directory))
    except OSError as exc:
        raise RuntimeError("runtime bundle bytecode cache is unavailable") from exc
    for entry in entries:
        path = Path(entry.path)
        try:
            info = path.lstat()
        except OSError as exc:
            raise RuntimeError("runtime bundle bytecode cache is unavailable") from exc
        if (
            _is_alias(info)
            or not stat.S_ISREG(info.st_mode)
            or path.relative_to(root).as_posix() not in allowed
        ):
            raise RuntimeError("runtime bundle bytecode cache contains an extra file")


def _physical_regular_file(path: Path, label: str) -> Path:
    candidate = path.absolute()
    try:
        info = candidate.lstat()
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise RuntimeError(f"{label} is unavailable") from exc
    if (
        not stat.S_ISREG(info.st_mode)
        or _is_alias(info)
        or os.path.normcase(os.fspath(candidate))
        != os.path.normcase(os.fspath(resolved))
    ):
        raise RuntimeError(f"{label} contains an alias")
    return resolved


def _physical_directory(path: Path, label: str) -> Path:
    candidate = path.absolute()
    try:
        info = candidate.lstat()
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise RuntimeError(f"{label} is unavailable") from exc
    if (
        not stat.S_ISDIR(info.st_mode)
        or _is_alias(info)
        or os.path.normcase(os.fspath(candidate))
        != os.path.normcase(os.fspath(resolved))
    ):
        raise RuntimeError(f"{label} contains an alias")
    return resolved


def _same_physical_file(left: Path, right: Path) -> bool:
    try:
        return os.path.samefile(left, _physical_regular_file(right, "runtime module"))
    except (OSError, RuntimeError):
        return False


def _same_physical_directory(left: Path, right: Path) -> bool:
    try:
        left_direct = _physical_directory(left, "runtime bundle")
        right_direct = _physical_directory(right, "runtime bundle")
        return os.path.samefile(left_direct, right_direct)
    except (OSError, RuntimeError):
        return False


def _same_resolved_path(left: Path, right: Path) -> bool:
    try:
        left_value = left.resolve(strict=True)
        right_value = right.resolve(strict=True)
    except OSError:
        return False
    return os.path.normcase(os.fspath(left_value)) == os.path.normcase(
        os.fspath(right_value)
    )


def _entry_exists(path: Path) -> bool:
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise RuntimeError("runtime repository is unavailable") from exc
    return True


def _is_alias(info: os.stat_result) -> bool:
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0) & _WINDOWS_REPARSE_POINT
    )


def main(
    argv: Sequence[str] | None = None,
    *,
    service: NormalizerService | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
    stdin: TextIO | None = None,
    installer: Installer | None = None,
    input_func: Callable[[str], str] | None = None,
) -> int:
    """Run one command with validation errors separated from degraded results."""
    output = sys.stdout if stdout is None else stdout
    errors = sys.stderr if stderr is None else stderr
    source = sys.stdin if stdin is None else stdin
    _configure_utf8(source, output, errors)
    try:
        args = build_parser().parse_args(argv)
    except _UsageError as exc:
        _write_text(errors, f"voice-intent: {exc}\n")
        return 2
    try:
        _validate_args(args)
    except _UsageError as exc:
        _write_text(errors, f"voice-intent: {exc}\n")
        return 2
    if args.command == "hook":
        from .hook import main as hook_main

        return hook_main(stdin=source, stdout=output, stderr=errors, service=service)
    try:
        active_service = default_service() if service is None else service
        active_installer = installer
        if active_installer is None:
            configured_paths = getattr(active_service, "paths", None)
            active_installer = default_installer(configured_paths)
        _complete_human_install_options(args, input_func)
        return _dispatch(args, active_service, active_installer, output)
    except Exception:
        return _degraded(args, output, "local_operation_unavailable")


def _dispatch(
    args: argparse.Namespace,
    service: NormalizerService,
    installer: Installer,
    output: TextIO,
) -> int:
    if args.command == "normalize":
        decision = service.normalize(
            NormalizeRequest(
                text=args.text,
                project_root=Path(args.project_root) if args.project_root else None,
                domains=tuple(args.domain),
                conversation_terms=tuple(args.conversation_term),
            )
        )
        _write_decision(output, decision, as_json=args.json)
        return 0
    if args.command == "learn":
        scope = Scope(args.scope)
        project_id = None
        if scope is Scope.PROJECT:
            with guard_project_root(args.project_root) as authority:
                project_id = service.paths.for_project(authority).project_id
        event = service.learning.confirm(args.alias, args.canonical, scope, project_id)
        return _write_result(output, args, {"status": "ok", "event": _event(event)})
    if args.command == "reject":
        event = service.learning.reject(args.alias, args.canonical)
        return _write_result(output, args, {"status": "ok", "event": _event(event)})
    if args.command == "undo":
        event = service.learning.undo_last()
        return _write_result(
            output,
            args,
            {"status": "ok", "event": None if event is None else _event(event)},
        )
    if args.command == "list":
        return _write_result(
            output,
            args,
            {
                "status": "ok",
                "events": [
                    _event(event) for event in service.learning.list_recent(args.limit)
                ],
            },
        )
    if args.command == "scan-project":
        with guard_project_root(args.project_root) as authority:
            result = scan_project(authority, service.paths)
        return _write_result(
            output,
            args,
            {
                "status": "ok",
                "entries": len(result.entries),
                "truncated": result.truncated,
                "files_scanned": result.files_scanned,
            },
        )
    if args.command == "doctor" and not args.platform and not args.all_detected:
        writable = _state_writable(service.paths.root)
        return _write_result(
            output,
            args,
            {
                "status": "ok" if writable else "degraded",
                "state_root": str(service.paths.root),
                "diagnostics": [] if writable else ["state_unavailable"],
            },
        )
    if args.command == "install":
        platforms = _selected_platforms(args, installer)
        results = installer.install(
            platforms,
            InstallOptions(
                strict=args.strict,
                output_dir=None if args.output_dir is None else Path(args.output_dir),
                workspace=None if args.workspace is None else Path(args.workspace),
                auto_update=bool(args.auto_update),
                implicit_invocation_confirmed=args.implicit_invocation_confirmed,
            ),
        )
        return _write_installer_results(output, args, results)
    if args.command == "uninstall":
        return _write_installer_results(
            output,
            args,
            installer.uninstall(
                _selected_platforms(args, installer),
                UninstallOptions(
                    remove_shared_data=args.remove_shared_data,
                    output_dir=None
                    if args.output_dir is None
                    else Path(args.output_dir),
                    workspace=None if args.workspace is None else Path(args.workspace),
                    strict=args.strict,
                ),
            ),
        )
    if args.command == "doctor":
        return _write_installer_results(
            output, args, installer.doctor(_selected_platforms(args, installer))
        )
    if args.command == "update":
        return _degraded(
            args, output, f"{args.command} is not configured in this build"
        )
    raise ValueError("unknown command")


def _write_decision(
    output: TextIO, decision: CorrectionDecision, *, as_json: bool
) -> None:
    if as_json:
        _write_json(output, _decision_payload(decision))
        return
    value = decision.question or decision.corrected_text
    _write_text(output, f"{value}\n")


def _decision_payload(decision: CorrectionDecision) -> dict[str, object]:
    return {
        "action": decision.action.value,
        "original_text": decision.original_text,
        "corrected_text": decision.corrected_text,
        "notices": list(decision.notices),
        "question": decision.question,
        "diagnostics": list(decision.diagnostics),
        "candidates": [_candidate(candidate) for candidate in decision.candidates],
    }


def _candidate(candidate: Candidate) -> dict[str, object]:
    return {
        "canonical": candidate.canonical,
        "original": candidate.original,
        "replacement_span": list(candidate.replacement_span),
        "score": candidate.score,
        "evidence": list(candidate.evidence),
        "scope": candidate.entry.scope.value,
    }


def _event(event: Any) -> dict[str, object]:
    raw = asdict(event)
    raw["status"] = event.status.value
    raw["scope"] = None if event.scope is None else event.scope.value
    return raw


def _write_result(
    output: TextIO, args: argparse.Namespace, payload: dict[str, object]
) -> int:
    if args.json:
        _write_json(output, payload)
    else:
        _write_text(output, _human_result(args, payload))
    return 0


def _degraded(args: argparse.Namespace, output: TextIO, diagnostic: str) -> int:
    payload = {"status": "degraded", "diagnostics": [diagnostic]}
    if getattr(args, "json", False):
        _write_json(output, payload)
    else:
        _write_text(output, f"Status: degraded\nDiagnostic: {diagnostic}\n")
    return 0


def _write_json(output: TextIO, payload: dict[str, object]) -> None:
    for ensure_ascii in (False, True):
        try:
            _write_text(
                output,
                json.dumps(payload, ensure_ascii=ensure_ascii, separators=(",", ":"))
                + "\n",
                fallback=False,
            )
            return
        except UnicodeError:
            continue


def _human_result(args: argparse.Namespace, payload: dict[str, object]) -> str:
    if args.command == "list":
        events = payload["events"]
        if not events:
            return "No learned mappings.\n"
        return "".join(
            f"{event['alias']} → {event['canonical']} ({event['scope']})\n"
            for event in events
        )
    if args.command == "doctor":
        diagnostics = payload["diagnostics"]
        lines = [
            f"Status: {payload['status']}",
            f"State root: {payload['state_root']}",
            (
                "State check: writable"
                if payload["status"] == "ok"
                else "State check: unavailable"
            ),
        ]
        lines.extend(f"Diagnostic: {item}" for item in diagnostics)
        return "\n".join(lines) + "\n"
    if args.command == "scan-project":
        suffix = " (truncated)" if payload["truncated"] else ""
        return (
            f"Scanned {payload['files_scanned']} files; found "
            f"{payload['entries']} terms{suffix}.\n"
        )
    return f"{payload['status']}\n"


def _validate_args(args: argparse.Namespace) -> None:
    if args.command == "list" and args.limit < 1:
        raise _UsageError("--limit must be positive")
    if args.command == "learn" and args.scope == "project" and not args.project_root:
        raise _UsageError("--project-root is required for project learning")
    if (
        args.command in {"install", "uninstall", "doctor"}
        and args.platform
        and args.all_detected
    ):
        raise _UsageError("--platform and --all-detected cannot be combined")
    if args.command in {"install", "uninstall"} and args.json:
        if not args.platform and not args.all_detected:
            raise _UsageError("--platform or --all-detected is required in JSON mode")
    if args.command == "install" and args.json and args.auto_update is None:
        raise _UsageError("--auto-update or --no-auto-update is required in JSON mode")


def _complete_human_install_options(
    args: argparse.Namespace, input_func: Callable[[str], str] | None
) -> None:
    if args.command != "install" or args.json:
        return
    ask = input if input_func is None else input_func
    if not args.platform and not args.all_detected:
        values = ask("Platforms (comma-separated): ")
        args.platform = [value.strip() for value in values.split(",") if value.strip()]
    if args.auto_update is None:
        response = (
            ask("Enable daily public hotword updates? [Y/n]: ").strip().casefold()
        )
        args.auto_update = response not in {"n", "no", "false", "0"}


def _selected_platforms(
    args: argparse.Namespace, installer: Installer
) -> tuple[str, ...]:
    if args.all_detected:
        return installer.detected()
    return tuple(args.platform)


def _write_installer_results(
    output: TextIO, args: argparse.Namespace, results: tuple[AdapterResult, ...]
) -> int:
    payload = [_adapter_result(result) for result in results]
    if args.json:
        _write_json(output, payload)
    else:
        for result in results:
            _write_text(
                output,
                f"{result.platform}: {result.status} ({result.capability.value})\n",
            )
            for message in result.messages:
                _write_text(output, f"  {message}\n")
    return 0


def _adapter_result(result: AdapterResult) -> dict[str, object]:
    return {
        "platform": result.platform,
        "status": result.status,
        "capability": result.capability.value,
        "messages": list(result.messages),
        "changed_paths": [str(path) for path in result.changed_paths],
    }


def _configure_utf8(*streams: object) -> None:
    for stream in streams:
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8")
            except Exception:
                continue


def _write_text(output: TextIO, value: str, *, fallback: bool = True) -> None:
    try:
        output.write(value)
    except UnicodeError:
        if not fallback:
            raise
        output.write(value.encode("ascii", "backslashreplace").decode("ascii"))


def _state_writable(root: Path) -> bool:
    target = root
    while not target.exists() and target != target.parent:
        target = target.parent
    return target.is_dir() and os.access(target, os.W_OK)
