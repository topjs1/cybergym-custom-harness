from __future__ import annotations

from .backend import DisabledDynamicBackend, DockerDynamicBackend, DynamicBackend, SidecarDynamicBackend
from .policy import strip_leaks
from .tools import DynamicTools

__all__ = [
    "DynamicBackend",
    "DisabledDynamicBackend",
    "DockerDynamicBackend",
    "SidecarDynamicBackend",
    "DynamicTools",
    "make_dynamic_backend",
    "strip_leaks",
]


def make_dynamic_backend(settings, workspace_root, runner=None) -> DynamicBackend:
    from .runner import SubprocessRunner

    if settings.dynamic_analysis == "off":
        return DisabledDynamicBackend()
    if settings.dynamic_analysis == "sidecar":
        return SidecarDynamicBackend(
            settings.dynamic_url,
            settings.dynamic_timeout_seconds,
            token=getattr(settings, "sidecar_token", ""),
        )
    return DockerDynamicBackend(settings, workspace_root, runner or SubprocessRunner())
