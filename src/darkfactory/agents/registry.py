from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from types import MappingProxyType

import yaml
from pydantic import ValidationError

import darkfactory.hooks as hook_exports
from darkfactory.agents.manifest import RoleManifest
from darkfactory.hooks.permission_gate import DENIED_ARGV_PREFIXES


DEFAULT_MANIFESTS_DIR = Path(__file__).resolve().parent / "manifests"
MANIFEST_EXTENSIONS = frozenset({".yaml", ".yml"})
_PROJECT_ROOT = Path(__file__).resolve().parents[3]
_PACKAGE_ROOT = Path(__file__).resolve().parents[1]
_DEFAULT_REGISTRY: Registry | None = None


class ManifestRegistryError(ValueError):
    """Raised when role manifests cannot be loaded into a registry."""


@dataclass(frozen=True, slots=True)
class Registry:
    _manifests: Mapping[str, RoleManifest]
    _source_paths: Mapping[str, Path]

    def __init__(
        self,
        manifests: Mapping[str, RoleManifest],
        source_paths: Mapping[str, Path] | None = None,
    ) -> None:
        copied = {
            role: manifest.model_copy(deep=True)
            for role, manifest in manifests.items()
        }
        object.__setattr__(self, "_manifests", MappingProxyType(copied))
        object.__setattr__(
            self,
            "_source_paths",
            MappingProxyType(dict(source_paths or {})),
        )

    def __len__(self) -> int:
        return len(self._manifests)

    @property
    def roles(self) -> tuple[str, ...]:
        return tuple(sorted(self._manifests))

    @property
    def hook_names(self) -> tuple[str, ...]:
        names = {
            attachment.name
            for manifest in self._manifests.values()
            for attachment in manifest.hooks
        }
        return tuple(sorted(names))

    def get(self, role: str) -> RoleManifest:
        try:
            manifest = self._manifests[role]
        except KeyError as exc:
            raise KeyError(f"unknown role manifest: {role}") from exc
        return manifest.model_copy(deep=True)

    def source_path(self, role: str) -> Path:
        try:
            return self._source_paths[role]
        except KeyError as exc:
            raise KeyError(f"unknown role manifest: {role}") from exc

    def role_owned_argv_table(self) -> Mapping[tuple[str, ...], frozenset[str]]:
        """Aggregate ``tools.role_owned_argv_prefixes`` across every manifest.

        Returns each role-owned argv prefix mapped to the frozen set of roles
        permitted to invoke it. The composer feeds this into
        ``make_permission_gate`` so each gate denies prefixes whose owning
        roles do not include the current one. Global denylists
        (``DENIED_ARGV_PREFIXES``, ``FORBIDDEN_TOKENS``, ``MERGE_TOOLS``)
        stay code-declared and are checked independently.
        """
        table: dict[tuple[str, ...], set[str]] = {}
        for role, manifest in self._manifests.items():
            for prefix in manifest.tools.role_owned_argv_prefixes:
                table.setdefault(tuple(prefix), set()).add(role)
        return MappingProxyType(
            {prefix: frozenset(roles) for prefix, roles in table.items()}
        )


def set_default_registry(registry: Registry) -> None:
    """Publish the immutable worker-startup registry for activity-time lookup."""
    global _DEFAULT_REGISTRY
    _DEFAULT_REGISTRY = registry


def get_default_registry() -> Registry:
    """Return the worker registry, lazily loading the default manifest dir."""
    global _DEFAULT_REGISTRY
    if _DEFAULT_REGISTRY is None:
        _DEFAULT_REGISTRY = load_registry(DEFAULT_MANIFESTS_DIR)
    return _DEFAULT_REGISTRY


def load_registry(manifests_dir: Path) -> Registry:
    manifests_dir = Path(manifests_dir)
    if not manifests_dir.is_dir():
        raise ManifestRegistryError(f"manifest directory not found: {manifests_dir}")

    available_hooks = _available_hook_names()
    manifests: dict[str, RoleManifest] = {}
    source_paths: dict[str, Path] = {}

    for path in _iter_manifest_files(manifests_dir):
        manifest = _load_manifest(path)
        role = manifest.identity.role
        if role in manifests:
            raise ManifestRegistryError(
                "duplicate role manifest "
                f"{role!r}: {source_paths[role]} and {path}"
            )
        _validate_hook_names(manifest, available_hooks, path)
        _validate_prompt_path(manifest, path)
        _validate_structured_output(manifest, path)
        _validate_role_owned_prefixes(manifest, path)
        manifests[role] = manifest
        source_paths[role] = path

    return Registry(manifests, source_paths)


def _iter_manifest_files(manifests_dir: Path) -> tuple[Path, ...]:
    return tuple(
        sorted(
            path
            for path in manifests_dir.iterdir()
            if path.is_file() and path.suffix in MANIFEST_EXTENSIONS
        )
    )


def _load_manifest(path: Path) -> RoleManifest:
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ManifestRegistryError(f"{path}: invalid YAML: {exc}") from exc

    if not isinstance(payload, dict):
        raise ManifestRegistryError(f"{path}: manifest must be a YAML mapping")

    try:
        return RoleManifest.model_validate(payload)
    except ValidationError as exc:
        raise ManifestRegistryError(f"{path}: invalid manifest: {exc}") from exc


def _available_hook_names() -> frozenset[str]:
    declared = getattr(hook_exports, "MANIFEST_HOOKS", None)
    if isinstance(declared, Mapping):
        return frozenset(str(name) for name in declared)
    exported = getattr(hook_exports, "__all__", ())
    return frozenset(
        name
        for name in exported
        if callable(getattr(hook_exports, name, None))
    )


def _validate_hook_names(
    manifest: RoleManifest,
    available_hooks: frozenset[str],
    path: Path,
) -> None:
    for attachment in manifest.hooks:
        if attachment.name not in available_hooks:
            known = ", ".join(sorted(available_hooks)) or "(none exported)"
            raise ManifestRegistryError(
                f"{path}: unknown hook {attachment.name!r}; known hooks: {known}"
            )


def _validate_prompt_path(manifest: RoleManifest, manifest_path: Path) -> None:
    resolved = resolve_prompt_path(manifest.llm.prompt_path)
    if not resolved.is_file():
        raise ManifestRegistryError(
            f"{manifest_path}: prompt_path {manifest.llm.prompt_path!r} "
            f"does not exist"
        )


def _validate_structured_output(manifest: RoleManifest, manifest_path: Path) -> None:
    declared = manifest.llm.structured_output
    if declared is None:
        return
    resolved = resolve_prompt_path(declared)
    if not resolved.is_file():
        raise ManifestRegistryError(
            f"{manifest_path}: structured_output {declared!r} does not exist"
        )


def _validate_role_owned_prefixes(manifest: RoleManifest, manifest_path: Path) -> None:
    """Refuse manifests that try to claim ownership of globally-denied argv.

    Aggregation rule: code-declared invariants ∪ manifest-declared role
    policies, with code unremovable. A manifest entry that starts with any
    ``DENIED_ARGV_PREFIXES`` row is a widening attempt — even though the gate
    re-checks denials at runtime, we refuse the misconfiguration here so the
    intent is visible at load time rather than masked by defense-in-depth.
    """
    for prefix in manifest.tools.role_owned_argv_prefixes:
        prefix_t = tuple(prefix)
        for denied in DENIED_ARGV_PREFIXES:
            if (
                len(prefix_t) >= len(denied)
                and prefix_t[: len(denied)] == denied
            ):
                raise ManifestRegistryError(
                    f"{manifest_path}: role_owned_argv_prefixes entry "
                    f"{list(prefix_t)!r} would widen the global denylist "
                    f"({list(denied)!r}); refused at load time."
                )


def resolve_prompt_path(prompt_path: str) -> Path:
    raw = Path(prompt_path)
    if raw.is_absolute():
        return raw

    candidates = (
        _PROJECT_ROOT / raw,
        _PACKAGE_ROOT / raw,
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return candidates[0]


@dataclass(frozen=True, slots=True)
class RoleSummary:
    role: str
    model: str
    prompt_path: Path
    # ``-1`` is the ``allowed: "all"`` sentinel (pure-yolo, no explicit
    # allowlist); otherwise the count of explicitly-listed tools.
    allowed_tool_count: int
    hook_names: tuple[str, ...]
    mcp_servers: tuple[str, ...]
    manifest_sha: str
    prompt_sha: str


def role_summaries(registry: Registry) -> tuple[RoleSummary, ...]:
    return tuple(_role_summary(registry, role) for role in registry.roles)


def _role_summary(registry: Registry, role: str) -> RoleSummary:
    manifest = registry.get(role)
    prompt_path = resolve_prompt_path(manifest.llm.prompt_path)
    return RoleSummary(
        role=role,
        model=manifest.llm.model,
        prompt_path=prompt_path,
        allowed_tool_count=(
            -1
            if manifest.tools.allowed == "all"
            else len(manifest.tools.allowed)
        ),
        hook_names=tuple(att.name for att in manifest.hooks),
        mcp_servers=tuple(manifest.mcp),
        manifest_sha=_file_sha(registry.source_path(role)),
        prompt_sha=_file_sha(prompt_path),
    )


def _file_sha(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest()
