# SPDX-FileCopyrightText: 2026 Kaushik Kumar <kaushikrjpm10@gmail.com>
# SPDX-License-Identifier: Apache-2.0

"""Writable resource and complete-file declaration types for direct skills."""

import uuid
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints


class SkillResource(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    path: str = Field(min_length=1, max_length=240)
    # 2 MiB decoded bytes may require 2,796,204 base64 characters.
    content: str = Field(max_length=2_796_204)
    encoding: Literal["utf-8", "base64"] = "utf-8"
    executable: bool = False


class SkillFileDeclaration(BaseModel):
    """Content-free manifest entry, including SKILL.md and legacy script."""

    model_config = ConfigDict(extra="forbid", strict=True)

    path: str = Field(min_length=1, max_length=240)
    # Read-only historical SKILL.md files may predate today's 2 MiB write cap.
    size: int = Field(ge=0, le=4 * 1024 * 1024)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    mode: Literal["0644", "0755"]


SkillRevision = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]


class SkillFileContents(BaseModel):
    """A single authorized, version-scoped file; binary content uses base64."""

    model_config = ConfigDict(extra="forbid", strict=True)

    version_id: uuid.UUID
    revision: SkillRevision
    file: SkillFileDeclaration
    content: str = Field(max_length=4 * 1024 * 1024)
    encoding: Literal["utf-8", "base64"]


class SkillVersionManifest(BaseModel):
    """Content-free index; file bytes are served individually after authorization."""

    model_config = ConfigDict(extra="forbid", strict=True)

    listing_id: uuid.UUID
    version_id: uuid.UUID
    revision: SkillRevision
    files: list[SkillFileDeclaration] = Field(max_length=130)


class SkillFilePut(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    action: Literal["put"]
    file: SkillResource


class SkillFileDelete(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    action: Literal["delete"]
    path: str = Field(min_length=1, max_length=240)


class SkillFileRename(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    action: Literal["rename"]
    path: str = Field(min_length=1, max_length=240)
    new_path: str = Field(min_length=1, max_length=240)


class SkillVersionRevisionRequest(BaseModel):
    """Compare one exact version's revision before changing its editor state."""

    model_config = ConfigDict(extra="forbid", strict=True)

    observed_revision: SkillRevision


class SkillFileOperations(BaseModel):
    """Atomic bounded patch: all operations compare the same observed revision."""

    model_config = ConfigDict(extra="forbid", strict=True)

    observed_revision: SkillRevision
    operations: list[SkillFilePut | SkillFileDelete | SkillFileRename] = Field(min_length=1, max_length=128)


class SkillFolderSnapshot(BaseModel):
    """Authoritative folder: scripts, including zero-byte files, are extras."""

    model_config = ConfigDict(extra="forbid", strict=True)

    skill_md_content: str = Field(min_length=1, max_length=2 * 1024 * 1024)
    extra_files: list[SkillResource] = Field(max_length=128)


class SkillSnapshotReplace(SkillFolderSnapshot):
    """Omission never inherits old files; git and legacy slots are cleared."""

    observed_revision: SkillRevision
