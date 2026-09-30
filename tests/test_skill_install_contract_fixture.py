# SPDX-FileCopyrightText: 2026 Kaushik Kumar <kaushikrjpm10@gmail.com>
# SPDX-License-Identifier: Apache-2.0

"""Frozen install fixture must remain byte-for-byte aligned with the server encoder."""

import base64
import hashlib
import json
import uuid
from pathlib import Path
from types import SimpleNamespace

from schemas.skill_resources import SkillInstallFolder
from services.skill_bundle import complete_skill_folder


def test_server_bundle_fixture_binds_selected_files_modes_and_v2_digest():
    example = json.loads((Path(__file__).parent / "fixtures" / "skill_folder_install_contract.json").read_text())
    expected = example["standalone"]["response_bundle"]
    parsed = SkillInstallFolder.model_validate_json(json.dumps(expected))
    assert expected["digest"].startswith("observal-content-v2:sha256:")
    assert expected["skill_file_path"] == ".pi/skills/example/SKILL.md"
    for file in parsed.files:
        data = base64.b64decode(file.content, validate=True)
        assert len(data) == file.size
        assert hashlib.sha256(data).hexdigest() == file.sha256
        assert file.version_id == parsed.version_id
    assert {file.path: file.mode for file in parsed.files} == {
        "SKILL.md": "0644",
        "assets/icon.bin": "0644",
        "scripts/run.sh": "0755",
    }

    row = SimpleNamespace(
        id=uuid.UUID(expected["version_id"]),
        version="1.1.0",
        description="Example skill",
        task_type="general",
        supported_harnesses=["pi"],
        delivery_mode="registry_direct",
        skill_md_content=base64.b64decode(parsed.files[0].content).decode(),
        script_filename=None,
        script_content=None,
        extra_files=[
            {"path": file.path, "content": file.content, "encoding": "base64", "executable": file.mode == "0755"}
            for file in parsed.files[1:]
        ],
    )
    actual = complete_skill_folder(uuid.UUID(expected["listing_id"]), row, skill_file_path=expected["skill_file_path"])
    assert json.loads(actual.model_dump_json()) == expected
