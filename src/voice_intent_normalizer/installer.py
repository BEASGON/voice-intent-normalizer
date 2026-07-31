"""Coordinator for one or more independent platform adapters."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from .adapters.base import (
    AdapterResult,
    CapabilityLevel,
    InstallOptions,
    PlatformAdapter,
    UninstallOptions,
)


class Installer:
    """Run requested adapters in caller order and isolate normal host failures."""

    def __init__(self, adapters: Mapping[str, PlatformAdapter]) -> None:
        self._adapters = dict(adapters)

    @property
    def platforms(self) -> tuple[str, ...]:
        return tuple(self._adapters)

    def detected(self) -> tuple[str, ...]:
        return tuple(
            name
            for name, adapter in self._adapters.items()
            if adapter.detect().capability is not CapabilityLevel.UNAVAILABLE
        )

    def install(
        self, platforms: Sequence[str], options: InstallOptions
    ) -> tuple[AdapterResult, ...]:
        return self._run("install", platforms, options)

    def doctor(self, platforms: Sequence[str]) -> tuple[AdapterResult, ...]:
        return self._run("doctor", platforms, None)

    def uninstall(
        self, platforms: Sequence[str], options: UninstallOptions
    ) -> tuple[AdapterResult, ...]:
        return self._run("uninstall", platforms, options)

    def _run(
        self, method: str, platforms: Sequence[str], options: object
    ) -> tuple[AdapterResult, ...]:
        results: list[AdapterResult] = []
        for name in platforms:
            adapter = self._adapters.get(name)
            if adapter is None:
                results.append(
                    AdapterResult(
                        name,
                        "unavailable",
                        CapabilityLevel.UNAVAILABLE,
                        ("unknown platform",),
                    )
                )
                continue
            try:
                operation = getattr(adapter, method)
                result = operation() if options is None else operation(options)
            except Exception:
                result = AdapterResult(
                    name,
                    "degraded",
                    CapabilityLevel.UNAVAILABLE,
                    ("platform operation unavailable",),
                )
            results.append(result)
            if getattr(options, "strict", False) and result.status in {
                "failed",
                "degraded",
            }:
                break
        return tuple(results)
