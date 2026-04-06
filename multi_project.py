"""
Multi-project registry: optional metaharness-projects.toml for one daemon managing many repos.

Control-plane root is the directory containing the registry file (or its parent when using
``--projects-file``). Discovery walks upward from a start path for ``metaharness-projects.toml``.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path

if sys.version_info >= (3, 11):
    import tomllib
else:
    try:
        import tomllib
    except ImportError:
        import tomli as tomllib  # type: ignore


DEFAULT_REGISTRY_FILENAME = "metaharness-projects.toml"


class MultiProjectRegistryError(ValueError):
    """Invalid or inconsistent multi-project registry contents."""


@dataclass
class RegistryProject:
    """One entry from ``[[projects]]`` after validation."""

    id: str
    root: Path  # resolved absolute
    enabled: bool = True
    label: str = ""


@dataclass
class MultiProjectRegistry:
    """Loaded ``metaharness-projects.toml`` with resolved project roots."""

    control_plane_root: Path
    registry_path: Path
    projects: list[RegistryProject] = field(default_factory=list)
    # If set, the daemon runs the product-agent loop only for this project id (see daemon).
    product_project_id: str | None = None


def find_registry_file(start: Path, *, filename: str = DEFAULT_REGISTRY_FILENAME) -> Path | None:
    """Walk upward from ``start`` until ``filename`` exists; return its path or ``None``."""
    current = start.resolve()
    for _ in range(64):
        candidate = current / filename
        if candidate.is_file():
            return candidate
        parent = current.parent
        if parent == current:
            break
        current = parent
    return None


def load_project_registry(
    control_plane_root: Path,
    *,
    registry_file: Path | None = None,
) -> MultiProjectRegistry | None:
    """
    Load ``metaharness-projects.toml`` under ``control_plane_root``.

    Returns ``None`` if the registry file is missing (caller uses single-project mode).
    Raises :class:`MultiProjectRegistryError` on invalid content.
    """
    base = control_plane_root.resolve()
    path = registry_file.resolve() if registry_file is not None else (base / DEFAULT_REGISTRY_FILENAME)
    if not path.is_file():
        return None
    reg_path = path.resolve()
    ctrl_root = reg_path.parent
    raw: dict
    with open(path, "rb") as f:
        raw = tomllib.load(f)

    projects_raw = raw.get("projects")
    if projects_raw is None:
        raise MultiProjectRegistryError("registry must contain a [[projects]] list")
    if not isinstance(projects_raw, list) or not projects_raw:
        raise MultiProjectRegistryError("[[projects]] must be a non-empty list")

    product_id = raw.get("product_project_id")
    product_project_id: str | None = None
    if product_id is not None and str(product_id).strip():
        product_project_id = str(product_id).strip()

    seen_ids: set[str] = set()
    projects: list[RegistryProject] = []

    for i, row in enumerate(projects_raw):
        if not isinstance(row, dict):
            raise MultiProjectRegistryError(f"projects[{i}] must be a table")
        pid = str(row.get("id", "") or "").strip()
        if not pid:
            raise MultiProjectRegistryError(f"projects[{i}] missing non-empty id")
        if pid in seen_ids:
            raise MultiProjectRegistryError(f"duplicate project id: {pid!r}")
        seen_ids.add(pid)

        root_str = str(row.get("root", "") or "").strip()
        if not root_str:
            raise MultiProjectRegistryError(f"project {pid!r} missing root")

        rel = Path(root_str)
        resolved = (reg_path.parent / rel).resolve() if not rel.is_absolute() else rel.resolve()

        if not resolved.is_dir():
            raise MultiProjectRegistryError(
                f"project {pid!r}: root is not an existing directory: {resolved}"
            )
        mh = resolved / "metaharness.toml"
        if not mh.is_file():
            raise MultiProjectRegistryError(
                f"project {pid!r}: no metaharness.toml at {resolved}"
            )

        enabled = bool(row.get("enabled", True))
        label = str(row.get("label", "") or "").strip()

        projects.append(
            RegistryProject(id=pid, root=resolved, enabled=enabled, label=label)
        )

    if product_project_id is not None and product_project_id not in seen_ids:
        raise MultiProjectRegistryError(
            f"product_project_id {product_project_id!r} does not match any project id"
        )

    return MultiProjectRegistry(
        control_plane_root=ctrl_root,
        registry_path=reg_path,
        projects=projects,
        product_project_id=product_project_id,
    )


def enabled_projects_in_order(registry: MultiProjectRegistry) -> list[RegistryProject]:
    """Stable file order, only ``enabled`` entries."""
    return [p for p in registry.projects if p.enabled]
