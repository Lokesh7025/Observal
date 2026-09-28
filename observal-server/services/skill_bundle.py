# SPDX-FileCopyrightText: 2026 Kaushik Kumar <kaushikrjpm10@gmail.com>
# SPDX-License-Identifier: Apache-2.0

"""Validate an effective (post-inheritance) direct skill bundle once, before persistence.

Routes must merge omitted fields from the previous version first; an explicitly
empty extra_files list clears inherited resources. Do not use this for git clones.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import ValidationError

from schemas.skill_resources import SkillFileDeclaration, SkillResource
from services.skill_validator import SkillValidationError, validate_skill_md_content_frontmatter

MAX_EXTRA_FILES = 128
MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_BUNDLE_BYTES = 4 * 1024 * 1024
MAX_PATH_BYTES = 240
MAX_SEGMENT_BYTES = 100
MAX_DEPTH = 12
_RESERVED = re.compile(r"(?:CON|PRN|AUX|NUL|CONIN\$|CONOUT\$|COM[1-9¹²³]|LPT[1-9¹²³])", re.IGNORECASE)
_UNSAFE = re.compile(r'[<>:"|?*\\]')


@dataclass(frozen=True)
class SkillBundleFile:
    path: str
    content: bytes
    executable: bool = False

    @property
    def declaration(self) -> SkillFileDeclaration:
        return SkillFileDeclaration(
            path=self.path,
            size=len(self.content),
            sha256=hashlib.sha256(self.content).hexdigest(),
            mode="0755" if self.executable else "0644",
        )


def validate_bundle_path(path: str) -> str:
    """Reject paths unsafe on POSIX, Windows, or case-insensitive filesystems."""
    if not isinstance(path, str) or not path or path.startswith("/") or unicodedata.normalize("NFC", path) != path:
        raise SkillValidationError("Resource path must be nonempty, relative and NFC normalized")
    try:
        raw = path.encode("utf-8")
        windows_length = len(path.encode("utf-16-le")) // 2
    except UnicodeError as exc:
        raise SkillValidationError("Resource path is not valid Unicode") from exc
    # The relative path must fit both the UTF-8 limit and Windows UTF-16
    # destination budget. Installers still check the full harness destination.
    if len(raw) > MAX_PATH_BYTES or windows_length > MAX_PATH_BYTES:
        raise SkillValidationError("Resource path exceeds 240 units")
    parts = path.split("/")
    if len(parts) > MAX_DEPTH:
        raise SkillValidationError("Resource path exceeds depth limit")
    for part in parts:
        if (
            part in {"", ".", ".."}
            or len(part.encode("utf-8")) > MAX_SEGMENT_BYTES
            or len(part.encode("utf-16-le")) // 2 > MAX_SEGMENT_BYTES
            or part.endswith((" ", "."))
            or _UNSAFE.search(part)
            or _RESERVED.fullmatch(part.partition(".")[0].rstrip(" "))
            or part.casefold() == ".git"
            or any(ord(c) < 32 or ord(c) == 127 or unicodedata.category(c) in {"Cc", "Cf"} for c in part)
        ):
            raise SkillValidationError("Unsafe resource path segment")
    return path


def _decode(resource: SkillResource) -> bytes:
    if resource.encoding == "utf-8":
        try:
            return resource.content.encode("utf-8")
        except UnicodeError as exc:
            raise SkillValidationError("Resource content is not valid UTF-8") from exc
    try:
        # Bound encoded input before allocating decoded bytes.
        if len(resource.content) > ((MAX_FILE_BYTES + 2) // 3) * 4 + 4:
            raise SkillValidationError("Resource exceeds per-file limit")
        return base64.b64decode(resource.content.encode("ascii"), validate=True)
    except (UnicodeError, binascii.Error) as exc:
        raise SkillValidationError("Invalid base64 resource content") from exc


def validate_skill_bundle(
    *,
    delivery_mode: Literal["registry_direct", "git_fetch"],
    skill_md_content: str | None,
    script_content: str | None = None,
    script_filename: str | None = None,
    extra_files: list[SkillResource | dict[str, Any]],
    enforce_limits: bool = True,
) -> tuple[SkillBundleFile, ...]:
    """Return the entire decoded file set; raise SkillValidationError on invalid input.

    Pass the effective extra_files list explicitly. An omitted request field
    inherits before calling this function; [] explicitly clears it. Only previously
    stored, unchanged versions may use enforce_limits=False; paths and decoding
    always receive validation. A git_fetch skill has no locally written bundle.
    """
    if not isinstance(extra_files, list):
        raise SkillValidationError("extra_files must be a list")
    if (script_content is None) != (script_filename is None):
        raise SkillValidationError("script_content and script_filename must both be set or cleared")
    if delivery_mode == "git_fetch":
        if extra_files:
            raise SkillValidationError("git_fetch cannot contain extra_files")
        # Preserve historical git_fetch metadata: the clone, not these inline
        # fields, determines the installed files. They must still be coherent.
        return ()
    if delivery_mode != "registry_direct":
        raise SkillValidationError("Unknown skill delivery mode")
    if not isinstance(skill_md_content, str) or not skill_md_content:
        raise SkillValidationError("registry_direct requires SKILL.md content")
    try:
        md = skill_md_content.encode("utf-8")
        validate_skill_md_content_frontmatter(skill_md_content)
    except UnicodeError as exc:
        raise SkillValidationError("SKILL.md is not valid UTF-8") from exc
    resources = extra_files
    if enforce_limits and len(resources) > MAX_EXTRA_FILES:
        raise SkillValidationError("Too many extra_files")
    files = [SkillBundleFile("SKILL.md", md)]
    if script_filename is not None:
        if not isinstance(script_filename, str):
            raise SkillValidationError("Invalid script filename")
        validate_bundle_path("scripts/" + script_filename)
        if "/" in script_filename:
            raise SkillValidationError("Legacy script filename must be one segment")
        try:
            content = script_content.encode("utf-8")  # type: ignore[union-attr]
        except (UnicodeError, AttributeError) as exc:
            raise SkillValidationError("Invalid legacy script content") from exc
        # Legacy CLI treats recognized script suffixes as executable.
        files.append(
            SkillBundleFile(
                "scripts/" + script_filename, content, script_filename.endswith((".sh", ".bash", ".py", ".rb"))
            )
        )
    for item in resources:
        try:
            resource = item if isinstance(item, SkillResource) else SkillResource.model_validate(item)
        except (ValidationError, ValueError, TypeError) as exc:
            raise SkillValidationError("Invalid extra_files entry") from exc
        validate_bundle_path(resource.path)
        content = _decode(resource)
        if enforce_limits and len(content) > MAX_FILE_BYTES:
            raise SkillValidationError("Resource exceeds per-file limit")
        files.append(SkillBundleFile(resource.path, content, resource.executable))
    seen: set[str] = set()
    for file in files:
        key = file.path.casefold()
        parts = key.split("/")
        if key in seen or any("/".join(parts[:index]) in seen for index in range(1, len(parts))):
            raise SkillValidationError("Duplicate or overlapping skill file paths")
        seen.add(key)
    if any(other.startswith(path + "/") for path in seen for other in seen):
        raise SkillValidationError("Skill file collides with a directory")
    if enforce_limits and sum(len(file.content) for file in files) > MAX_BUNDLE_BYTES:
        raise SkillValidationError("Skill bundle exceeds decoded size limit")
    return tuple(files)
