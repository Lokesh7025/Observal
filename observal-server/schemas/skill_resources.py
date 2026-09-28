# SPDX-FileCopyrightText: 2026 Kaushik Kumar <kaushikrjpm10@gmail.com>
# SPDX-License-Identifier: Apache-2.0

"""Writable resource and complete-file declaration types for direct skills."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class SkillResource(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    path: str
    content: str
    encoding: Literal["utf-8", "base64"] = "utf-8"
    executable: bool = False


class SkillFileDeclaration(BaseModel):
    """Content-free manifest entry, including SKILL.md and legacy script."""

    model_config = ConfigDict(extra="forbid", strict=True)

    path: str
    size: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    mode: Literal["0644", "0755"]
