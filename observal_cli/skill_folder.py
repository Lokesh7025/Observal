# SPDX-FileCopyrightText: 2026 Shree Harini <shree@observal.dev>
# SPDX-License-Identifier: Apache-2.0

"""Verified skill folder bundle validation and installation.

This module implements secure installation of complete skill folders from the
registry. It validates all bundle metadata, verifies file integrity, stages
files atomically, and handles collision detection.

Security requirements (per implementation-1730.md):
- Validate complete response before ANY destination write
- Require exactly one SKILL.md per bundle
- All file version IDs must equal selected version UUID
- Verify base64, SHA-256, size, mode for each file
- Reject path traversal, symlinks, case/Unicode collisions
- Stage to private sibling tree before modifying active path
- Never downgrade a v2 pin to v1

Contract reference: tests/fixtures/skill_folder_install_contract.json
"""

from __future__ import annotations

import base64
import hashlib
import os
import shutil
import stat
import unicodedata
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

from loguru import logger as optic

if TYPE_CHECKING:
    from typing import Any

# ── Constants ───────────────────────────────────────────────────────────────

SUPPORTED_FEATURE = "skill_extra_files_v1"

# Bundle limits (from server contract)
MAX_EXTRA_FILES = 128
MAX_FILE_SIZE = 2 * 1024 * 1024  # 2 MiB per file
MAX_TREE_SIZE = 4 * 1024 * 1024  # 4 MiB total decoded
MAX_PATH_BYTES = 240  # UTF-8 bytes total
MAX_PATH_UTF16 = 240  # UTF-16 units total
MAX_SEGMENT_LEN = 100  # per path segment
MAX_DEPTH = 12

VALID_MODES = frozenset(("0644", "0755"))
SKILL_MD_NAME = "SKILL.md"

# Case-insensitive reserved names (Windows + macOS)
_RESERVED_NAMES = frozenset(
    name.upper()
    for name in (
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{i}" for i in range(1, 10)),
        *(f"LPT{i}" for i in range(1, 10)),
        ".DS_Store",
        "Thumbs.db",
        "desktop.ini",
    )
)

# Forbidden path segments
_FORBIDDEN_SEGMENTS = frozenset((".", "..", ".git"))


# ── Data structures ─────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class BundleFile:
    """Validated bundle file ready for installation."""

    path: str  # Relative POSIX path within skill folder
    content: bytes  # Decoded content
    sha256: str  # Expected SHA-256 hex
    size: int  # Expected size
    mode: int  # File mode (0o644 or 0o755)
    version_id: str  # Version UUID this file belongs to


@dataclass(frozen=True, slots=True)
class ValidatedBundle:
    """Completely validated skill folder bundle."""

    listing_id: str
    version_id: str
    digest: str
    skill_file_path: str  # Harness-relative path to SKILL.md
    folder_name: str  # Declared folder name (from SKILL.md name)
    files: tuple[BundleFile, ...]
    total_size: int


class BundleValidationError(Exception):
    """Bundle validation failed with a specific reason."""

    def __init__(self, message: str, *, recoverable: bool = False):
        super().__init__(message)
        self.recoverable = recoverable


class BundleInstallError(Exception):
    """Bundle installation failed."""

    pass


# ── Path validation ─────────────────────────────────────────────────────────


def _normalize_path(path: str) -> str:
    """NFC-normalize and validate a relative POSIX path."""
    # Normalize to NFC (canonical decomposition then composition)
    normalized = unicodedata.normalize("NFC", path)

    # Must not be empty
    if not normalized:
        raise BundleValidationError("Empty path in bundle")

    # Parse as POSIX path
    posix = PurePosixPath(normalized)

    # Must be relative (no leading slash, no drive)
    if posix.is_absolute():
        raise BundleValidationError(f"Absolute path not allowed: {path}")

    # Check each segment
    parts = posix.parts
    if not parts:
        raise BundleValidationError(f"Path resolves to empty after normalization: {path}")
    if len(parts) > MAX_DEPTH:
        raise BundleValidationError(f"Path too deep ({len(parts)} > {MAX_DEPTH}): {path}")

    for segment in parts:
        # Forbidden segments
        if segment in _FORBIDDEN_SEGMENTS:
            raise BundleValidationError(f"Forbidden path segment '{segment}' in: {path}")

        # Reserved names (case-insensitive, with or without extension)
        name_upper = segment.upper()
        base_name = name_upper.split(".")[0] if "." in name_upper else name_upper
        if base_name in _RESERVED_NAMES or name_upper in _RESERVED_NAMES:
            raise BundleValidationError(f"Reserved filename '{segment}' in: {path}")

        # Segment length
        if len(segment) > MAX_SEGMENT_LEN:
            raise BundleValidationError(f"Path segment too long ({len(segment)} > {MAX_SEGMENT_LEN}): {segment}")

        # No trailing/leading spaces or dots
        if segment != segment.strip() or segment.endswith("."):
            raise BundleValidationError(f"Invalid path segment (trailing space/dot): {segment}")

    # Reconstruct canonical path from parts (removes ./)
    canonical = str(posix)

    # Total path length checks (on canonical form)
    path_bytes = canonical.encode("utf-8")
    if len(path_bytes) > MAX_PATH_BYTES:
        raise BundleValidationError(f"Path too long ({len(path_bytes)} UTF-8 bytes > {MAX_PATH_BYTES}): {path}")

    path_utf16_units = len(canonical.encode("utf-16-le")) // 2
    if path_utf16_units > MAX_PATH_UTF16:
        raise BundleValidationError(f"Path too long ({path_utf16_units} UTF-16 units > {MAX_PATH_UTF16}): {path}")

    return canonical


def _case_fold_key(path: str) -> str:
    """Return a case-folded key for collision detection."""
    return unicodedata.normalize("NFC", path.casefold())


# ── Bundle validation ───────────────────────────────────────────────────────


def validate_bundle(
    bundle: dict[str, Any],
    *,
    expected_version_id: str | None = None,
    expected_digest: str | None = None,
) -> ValidatedBundle:
    """Validate a complete skill folder bundle from server response.

    Args:
        bundle: Raw bundle dict from server (standalone or from skill_bundles)
        expected_version_id: If set, all files must have this version_id
        expected_digest: If set, bundle digest must match (for pinned installs)

    Returns:
        ValidatedBundle with verified files ready for installation

    Raises:
        BundleValidationError: If validation fails
    """
    optic.debug("Validating skill folder bundle")

    # Required top-level fields
    listing_id = bundle.get("listing_id")
    version_id = bundle.get("version_id")
    digest = bundle.get("digest")
    skill_file_path = bundle.get("skill_file_path")
    files_raw = bundle.get("files")

    if not listing_id:
        raise BundleValidationError("Bundle missing listing_id")
    if not version_id:
        raise BundleValidationError("Bundle missing version_id")
    if not digest:
        raise BundleValidationError("Bundle missing digest")
    if not skill_file_path:
        raise BundleValidationError("Bundle missing skill_file_path")
    if not isinstance(files_raw, list):
        raise BundleValidationError("Bundle missing or invalid files array")

    # Verify expected values if provided
    if expected_version_id and str(version_id) != str(expected_version_id):
        raise BundleValidationError(
            f"Bundle version_id mismatch: expected {expected_version_id}, got {version_id}"
        )
    if expected_digest and digest != expected_digest:
        raise BundleValidationError(
            f"Bundle digest mismatch: expected {expected_digest}, got {digest}",
            recoverable=False,
        )

    # File count check
    if len(files_raw) == 0:
        raise BundleValidationError("Bundle has no files")
    if len(files_raw) > MAX_EXTRA_FILES + 1:  # +1 for SKILL.md
        raise BundleValidationError(f"Too many files ({len(files_raw)} > {MAX_EXTRA_FILES + 1})")

    # Validate each file and check for collisions
    validated_files: list[BundleFile] = []
    seen_paths: dict[str, str] = {}  # case-folded -> original
    has_skill_md = False
    total_size = 0

    for i, file_raw in enumerate(files_raw):
        try:
            vfile = _validate_file(file_raw, version_id)
        except BundleValidationError as e:
            raise BundleValidationError(f"File {i}: {e}") from e

        # Check for SKILL.md
        if vfile.path == SKILL_MD_NAME:
            has_skill_md = True
            # SKILL.md must not be executable
            if vfile.mode == 0o755:
                raise BundleValidationError("SKILL.md must not be executable")

        # Case-fold collision check
        fold_key = _case_fold_key(vfile.path)
        if fold_key in seen_paths:
            raise BundleValidationError(
                f"Path collision (case-insensitive): '{vfile.path}' vs '{seen_paths[fold_key]}'"
            )
        seen_paths[fold_key] = vfile.path

        # Track total size
        total_size += vfile.size
        if total_size > MAX_TREE_SIZE:
            raise BundleValidationError(f"Total bundle size exceeds {MAX_TREE_SIZE} bytes")

        validated_files.append(vfile)

    if not has_skill_md:
        raise BundleValidationError("Bundle must contain exactly one SKILL.md")

    # Extract folder name from skill_file_path
    # e.g., ".pi/skills/example/SKILL.md" -> "example"
    skill_path = PurePosixPath(skill_file_path)
    if skill_path.name != SKILL_MD_NAME:
        raise BundleValidationError(f"skill_file_path must end with SKILL.md: {skill_file_path}")
    folder_name = skill_path.parent.name
    if not folder_name:
        raise BundleValidationError(f"Cannot extract folder name from skill_file_path: {skill_file_path}")

    optic.debug(
        "Bundle validated: listing_id={}, version_id={}, files={}, size={}",
        listing_id,
        version_id,
        len(validated_files),
        total_size,
    )

    return ValidatedBundle(
        listing_id=str(listing_id),
        version_id=str(version_id),
        digest=str(digest),
        skill_file_path=str(skill_file_path),
        folder_name=folder_name,
        files=tuple(validated_files),
        total_size=total_size,
    )


def _validate_file(file_raw: dict[str, Any], expected_version_id: str) -> BundleFile:
    """Validate a single file entry from the bundle."""
    path = file_raw.get("path")
    content_b64 = file_raw.get("content")
    sha256_expected = file_raw.get("sha256")
    size_expected = file_raw.get("size")
    mode_str = file_raw.get("mode")
    file_version_id = file_raw.get("version_id")
    encoding = file_raw.get("encoding", "base64")

    # Required fields
    if not path:
        raise BundleValidationError("Missing path")
    if content_b64 is None:
        raise BundleValidationError(f"Missing content for {path}")
    if not sha256_expected:
        raise BundleValidationError(f"Missing sha256 for {path}")
    if size_expected is None:
        raise BundleValidationError(f"Missing size for {path}")
    if not mode_str:
        raise BundleValidationError(f"Missing mode for {path}")
    if not file_version_id:
        raise BundleValidationError(f"Missing version_id for {path}")

    # Version ID must match bundle
    if str(file_version_id) != str(expected_version_id):
        raise BundleValidationError(
            f"File version_id mismatch for {path}: expected {expected_version_id}, got {file_version_id}"
        )

    # Validate path
    normalized_path = _normalize_path(path)

    # Validate mode
    if mode_str not in VALID_MODES:
        raise BundleValidationError(f"Invalid mode '{mode_str}' for {path}")
    mode = 0o755 if mode_str == "0755" else 0o644

    # Validate encoding
    if encoding != "base64":
        raise BundleValidationError(f"Unsupported encoding '{encoding}' for {path}")

    # Decode content
    try:
        content = base64.b64decode(content_b64, validate=True)
    except Exception as e:
        raise BundleValidationError(f"Invalid base64 content for {path}: {e}") from e

    # Verify size
    if len(content) != size_expected:
        raise BundleValidationError(
            f"Size mismatch for {path}: expected {size_expected}, got {len(content)}"
        )

    # Check individual file size limit
    if len(content) > MAX_FILE_SIZE:
        raise BundleValidationError(f"File too large ({len(content)} > {MAX_FILE_SIZE}): {path}")

    # Verify SHA-256
    actual_sha256 = hashlib.sha256(content).hexdigest()
    if actual_sha256 != sha256_expected:
        raise BundleValidationError(
            f"SHA-256 mismatch for {path}: expected {sha256_expected}, got {actual_sha256}"
        )

    return BundleFile(
        path=normalized_path,
        content=content,
        sha256=sha256_expected,
        size=len(content),
        mode=mode,
        version_id=str(file_version_id),
    )


# ── Collision detection ─────────────────────────────────────────────────────


def detect_destination_collisions(
    target_dir: Path,
    bundle: ValidatedBundle,
    *,
    existing_skills: list[Path] | None = None,
    bundled_skills: list[str] | None = None,
) -> list[str]:
    """Check for collisions with existing installations.

    Args:
        target_dir: Where the skill folder would be installed
        bundle: Validated bundle
        existing_skills: Paths to other installed skill folders
        bundled_skills: Names of bundled Observal skills (reserved)

    Returns:
        List of collision warning messages (empty if none)
    """
    collisions = []

    # Check if target already exists
    if target_dir.exists():
        if target_dir.is_symlink():
            collisions.append(f"Target is a symlink: {target_dir}")
        elif target_dir.is_dir():
            # Check if it's a managed skill folder
            skill_md = target_dir / SKILL_MD_NAME
            if skill_md.exists():
                collisions.append(f"Skill already exists at: {target_dir}")
            else:
                collisions.append(f"Non-skill directory exists at: {target_dir}")
        else:
            collisions.append(f"File exists at skill destination: {target_dir}")

    # Check bundled skill names
    if bundled_skills and bundle.folder_name in bundled_skills:
        collisions.append(f"Folder name '{bundle.folder_name}' conflicts with bundled Observal skill")

    # Check other installed skills for case collisions
    if existing_skills:
        fold_key = _case_fold_key(bundle.folder_name)
        for skill_path in existing_skills:
            if skill_path == target_dir:
                continue
            existing_fold = _case_fold_key(skill_path.name)
            if existing_fold == fold_key and skill_path.name != bundle.folder_name:
                collisions.append(
                    f"Case-insensitive collision: '{bundle.folder_name}' vs existing '{skill_path.name}'"
                )

    return collisions


# ── Atomic installation ─────────────────────────────────────────────────────


def install_folder_bundle(
    bundle: ValidatedBundle,
    target_dir: Path,
    *,
    backup_dir: Path | None = None,
    force: bool = False,
) -> Path:
    """Install a validated bundle atomically with rollback support.

    Args:
        bundle: Validated bundle to install
        target_dir: Destination directory (e.g., ~/.claude-code/skills/example)
        backup_dir: Where to back up existing content (if target exists)
        force: If True, overwrite existing without backup prompt

    Returns:
        Path to installed SKILL.md

    Raises:
        BundleInstallError: If installation fails
    """
    optic.info("Installing skill folder bundle to {}", target_dir)

    # Create parent if needed
    target_dir.parent.mkdir(parents=True, exist_ok=True)

    # Stage directory (private sibling)
    stage_dir = target_dir.parent / f".{target_dir.name}.stage.{os.getpid()}"

    try:
        # Clean up any stale stage from previous interrupted install
        if stage_dir.exists():
            shutil.rmtree(stage_dir)

        # Create staging directory
        stage_dir.mkdir(mode=0o700)

        # Write all files to staging
        for file in bundle.files:
            file_path = stage_dir / file.path
            file_path.parent.mkdir(parents=True, exist_ok=True)

            # Check for symlink attacks in stage
            if file_path.parent.resolve() != (stage_dir / PurePosixPath(file.path).parent).resolve():
                raise BundleInstallError(f"Path traversal detected during staging: {file.path}")

            file_path.write_bytes(file.content)
            os.chmod(file_path, file.mode)

        # Verify staged files
        for file in bundle.files:
            staged_file = stage_dir / file.path
            if not staged_file.is_file():
                raise BundleInstallError(f"Staged file missing: {file.path}")
            if staged_file.stat().st_size != file.size:
                raise BundleInstallError(f"Staged file size mismatch: {file.path}")
            actual_mode = stat.S_IMODE(staged_file.stat().st_mode)
            if actual_mode != file.mode:
                raise BundleInstallError(f"Staged file mode mismatch: {file.path}")

        # Handle existing target
        backup_path = None
        if target_dir.exists() or target_dir.is_symlink():
            if target_dir.is_symlink():
                # Remove symlink, don't try to back up
                target_dir.unlink()
            elif force or backup_dir:
                # Backup existing
                backup_path = backup_dir or target_dir.parent / f".{target_dir.name}.backup.{os.getpid()}"
                if backup_path.exists():
                    shutil.rmtree(backup_path)
                target_dir.rename(backup_path)
                optic.debug("Backed up existing skill to {}", backup_path)
            else:
                raise BundleInstallError(
                    f"Target exists and no backup specified: {target_dir}. "
                    "Use --force or provide a backup location."
                )

        # Atomic swap: rename stage to target
        try:
            stage_dir.rename(target_dir)
        except OSError as e:
            # Restore backup on failure
            if backup_path and backup_path.exists():
                optic.error("Swap failed, restoring backup")
                if target_dir.exists():
                    shutil.rmtree(target_dir)
                backup_path.rename(target_dir)
            raise BundleInstallError(f"Failed to install skill folder: {e}") from e

        # Remove backup on success (if we created one)
        if backup_path and backup_path.exists() and backup_dir is None:
            shutil.rmtree(backup_path)

        optic.info("Skill folder installed successfully: {}", target_dir)
        return target_dir / SKILL_MD_NAME

    except BundleInstallError:
        raise
    except Exception as e:
        raise BundleInstallError(f"Unexpected error during installation: {e}") from e
    finally:
        # Clean up staging directory if it still exists
        if stage_dir.exists():
            try:
                shutil.rmtree(stage_dir)
            except OSError:
                pass


def uninstall_folder(target_dir: Path, *, backup_dir: Path | None = None) -> bool:
    """Remove an installed skill folder.

    Args:
        target_dir: Skill folder to remove
        backup_dir: Where to move it (instead of deleting)

    Returns:
        True if removed, False if not found
    """
    if not target_dir.exists():
        return False

    if target_dir.is_symlink():
        target_dir.unlink()
        return True

    if backup_dir:
        backup_dir.parent.mkdir(parents=True, exist_ok=True)
        if backup_dir.exists():
            shutil.rmtree(backup_dir)
        target_dir.rename(backup_dir)
    else:
        shutil.rmtree(target_dir)

    return True
