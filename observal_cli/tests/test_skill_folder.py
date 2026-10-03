# SPDX-FileCopyrightText: 2026 Shree Harini <shree@observal.dev>
# SPDX-License-Identifier: Apache-2.0

"""Tests for skill folder bundle validation and installation."""

from __future__ import annotations

import base64
import hashlib
import os
import tempfile
from pathlib import Path

import pytest

from observal_cli.skill_folder import (
    BundleInstallError,
    BundleValidationError,
    DirectoryCaptureError,
    _case_fold_key,
    _normalize_path,
    capture_directory,
    detect_destination_collisions,
    install_folder_bundle,
    snapshot_to_extra_files,
    validate_bundle,
)


def _make_file(path: str, content: bytes, mode: str = "0644") -> dict:
    """Create a file entry for a test bundle."""
    return {
        "path": path,
        "content": base64.b64encode(content).decode(),
        "sha256": hashlib.sha256(content).hexdigest(),
        "size": len(content),
        "mode": mode,
        "version_id": "test-version-id",
        "encoding": "base64",
    }


def _make_bundle(files: list[dict], version_id: str = "test-version-id") -> dict:
    """Create a complete test bundle."""
    return {
        "listing_id": "test-listing-id",
        "version_id": version_id,
        "digest": "observal-content-v2:sha256:test",
        "skill_file_path": ".pi/skills/example/SKILL.md",
        "files": files,
    }


class TestPathNormalization:
    """Tests for path validation and normalization."""

    def test_valid_path(self):
        assert _normalize_path("scripts/run.sh") == "scripts/run.sh"

    def test_absolute_path_rejected(self):
        with pytest.raises(BundleValidationError, match="Absolute path"):
            _normalize_path("/etc/passwd")

    def test_traversal_rejected(self):
        with pytest.raises(BundleValidationError, match="Forbidden path segment"):
            _normalize_path("../secret.txt")

    def test_relative_path_normalized(self):
        # Relative path components like ./foo are normalized
        assert _normalize_path("./foo/bar.txt") == "foo/bar.txt"

    def test_git_segment_rejected(self):
        with pytest.raises(BundleValidationError, match="Forbidden path segment"):
            _normalize_path(".git/config")

    def test_reserved_name_rejected(self):
        with pytest.raises(BundleValidationError, match="Reserved filename"):
            _normalize_path("CON")

    def test_deep_path_rejected(self):
        deep_path = "/".join(["a"] * 15)
        with pytest.raises(BundleValidationError, match="too deep"):
            _normalize_path(deep_path)

    def test_long_segment_rejected(self):
        long_segment = "a" * 150
        with pytest.raises(BundleValidationError, match="segment too long"):
            _normalize_path(long_segment)

    def test_unicode_normalized(self):
        # NFC normalization test
        path = "café.txt"  # Pre-composed
        result = _normalize_path(path)
        assert result == path


class TestCaseFoldKey:
    """Tests for case-insensitive collision detection."""

    def test_lowercase(self):
        assert _case_fold_key("ABC") == _case_fold_key("abc")

    def test_different_strings(self):
        assert _case_fold_key("foo") != _case_fold_key("bar")


class TestBundleValidation:
    """Tests for complete bundle validation."""

    def test_valid_minimal_bundle(self):
        skill_md = b"---\nname: test\n---\n# Test"
        bundle = _make_bundle([_make_file("SKILL.md", skill_md)])
        result = validate_bundle(bundle)

        assert result.listing_id == "test-listing-id"
        assert result.version_id == "test-version-id"
        assert result.folder_name == "example"
        assert len(result.files) == 1
        assert result.files[0].path == "SKILL.md"

    def test_valid_multi_file_bundle(self):
        skill_md = b"---\nname: test\n---\n# Test"
        script = b"#!/bin/sh\necho hello"
        bundle = _make_bundle([
            _make_file("SKILL.md", skill_md),
            _make_file("scripts/run.sh", script, "0755"),
        ])
        result = validate_bundle(bundle)

        assert len(result.files) == 2
        assert result.total_size == len(skill_md) + len(script)

    def test_missing_skill_md(self):
        bundle = _make_bundle([_make_file("scripts/run.sh", b"echo hi")])
        with pytest.raises(BundleValidationError, match=r"must contain exactly one SKILL\.md"):
            validate_bundle(bundle)

    def test_executable_skill_md_rejected(self):
        skill_md = b"---\nname: test\n---\n# Test"
        bundle = _make_bundle([_make_file("SKILL.md", skill_md, "0755")])
        with pytest.raises(BundleValidationError, match="must not be executable"):
            validate_bundle(bundle)

    def test_version_id_mismatch(self):
        skill_md = b"---\nname: test\n---\n# Test"
        bundle = _make_bundle([_make_file("SKILL.md", skill_md)])
        with pytest.raises(BundleValidationError, match="version_id mismatch"):
            validate_bundle(bundle, expected_version_id="different-id")

    def test_digest_mismatch(self):
        skill_md = b"---\nname: test\n---\n# Test"
        bundle = _make_bundle([_make_file("SKILL.md", skill_md)])
        with pytest.raises(BundleValidationError, match="digest mismatch"):
            validate_bundle(bundle, expected_digest="observal-content-v2:sha256:different")

    def test_sha256_mismatch(self):
        skill_md = b"---\nname: test\n---\n# Test"
        file_entry = _make_file("SKILL.md", skill_md)
        file_entry["sha256"] = "0" * 64
        bundle = _make_bundle([file_entry])
        with pytest.raises(BundleValidationError, match="SHA-256 mismatch"):
            validate_bundle(bundle)

    def test_size_mismatch(self):
        skill_md = b"---\nname: test\n---\n# Test"
        file_entry = _make_file("SKILL.md", skill_md)
        file_entry["size"] = 999
        bundle = _make_bundle([file_entry])
        with pytest.raises(BundleValidationError, match="Size mismatch"):
            validate_bundle(bundle)

    def test_invalid_base64(self):
        bundle = _make_bundle([{
            "path": "SKILL.md",
            "content": "not-valid-base64!!!",
            "sha256": "test",
            "size": 10,
            "mode": "0644",
            "version_id": "test-version-id",
            "encoding": "base64",
        }])
        with pytest.raises(BundleValidationError, match="Invalid base64"):
            validate_bundle(bundle)

    def test_case_collision_rejected(self):
        skill_md = b"---\nname: test\n---\n# Test"
        bundle = _make_bundle([
            _make_file("SKILL.md", skill_md),
            _make_file("README.md", b"readme"),
            _make_file("readme.md", b"readme2"),  # Case collision
        ])
        with pytest.raises(BundleValidationError, match=r"collision.*case-insensitive"):
            validate_bundle(bundle)

    def test_empty_bundle_rejected(self):
        bundle = _make_bundle([])
        with pytest.raises(BundleValidationError, match="no files"):
            validate_bundle(bundle)


class TestCollisionDetection:
    """Tests for destination collision detection."""

    def test_no_collision_empty_target(self):
        skill_md = b"---\nname: test\n---\n# Test"
        bundle = _make_bundle([_make_file("SKILL.md", skill_md)])
        validated = validate_bundle(bundle)

        with tempfile.TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "new-skill"
            collisions = detect_destination_collisions(target, validated)
            assert collisions == []

    def test_collision_existing_skill(self):
        skill_md = b"---\nname: test\n---\n# Test"
        bundle = _make_bundle([_make_file("SKILL.md", skill_md)])
        validated = validate_bundle(bundle)

        with tempfile.TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "existing-skill"
            target.mkdir()
            (target / "SKILL.md").write_text("existing")
            collisions = detect_destination_collisions(target, validated)
            assert len(collisions) == 1
            assert "already exists" in collisions[0]

    def test_collision_bundled_skill(self):
        skill_md = b"---\nname: observal\n---\n# Test"
        bundle = _make_bundle([_make_file("SKILL.md", skill_md)])
        bundle["skill_file_path"] = ".pi/skills/observal/SKILL.md"
        validated = validate_bundle(bundle)

        with tempfile.TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "observal"
            collisions = detect_destination_collisions(
                target, validated, bundled_skills=["observal"]
            )
            assert len(collisions) == 1
            assert "bundled" in collisions[0].lower()


class TestFolderInstallation:
    """Tests for atomic folder installation."""

    def test_install_minimal(self):
        skill_md = b"---\nname: test\n---\n# Test"
        bundle = _make_bundle([_make_file("SKILL.md", skill_md)])
        validated = validate_bundle(bundle)

        with tempfile.TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "test-skill"
            result = install_folder_bundle(validated, target)

            assert result == target / "SKILL.md"
            assert (target / "SKILL.md").read_bytes() == skill_md

    def test_install_multi_file(self):
        skill_md = b"---\nname: test\n---\n# Test"
        script = b"#!/bin/sh\necho hello"
        bundle = _make_bundle([
            _make_file("SKILL.md", skill_md),
            _make_file("scripts/run.sh", script, "0755"),
        ])
        validated = validate_bundle(bundle)

        with tempfile.TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "test-skill"
            result = install_folder_bundle(validated, target)

            assert result == target / "SKILL.md"
            assert (target / "SKILL.md").read_bytes() == skill_md
            assert (target / "scripts" / "run.sh").read_bytes() == script

            # Check executable mode
            mode = os.stat(target / "scripts" / "run.sh").st_mode
            assert mode & 0o111  # Has execute bits

    def test_install_force_overwrite(self):
        skill_md = b"---\nname: test\n---\n# Test"
        bundle = _make_bundle([_make_file("SKILL.md", skill_md)])
        validated = validate_bundle(bundle)

        with tempfile.TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "test-skill"
            target.mkdir()
            (target / "SKILL.md").write_text("old content")

            result = install_folder_bundle(validated, target, force=True)

            assert result == target / "SKILL.md"
            assert (target / "SKILL.md").read_bytes() == skill_md

    def test_install_fails_without_force(self):
        skill_md = b"---\nname: test\n---\n# Test"
        bundle = _make_bundle([_make_file("SKILL.md", skill_md)])
        validated = validate_bundle(bundle)

        with tempfile.TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "test-skill"
            target.mkdir()
            (target / "SKILL.md").write_text("old content")

            with pytest.raises(BundleInstallError, match="exists"):
                install_folder_bundle(validated, target, force=False)


class TestDirectoryCapture:
    """Tests for capturing local directories for upload."""

    def test_capture_minimal(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            source = Path(tmpdir)
            (source / "SKILL.md").write_text("---\nname: test\n---\n# Test")

            snapshot = capture_directory(source)

            assert "name: test" in snapshot.skill_md_content
            assert len(snapshot.extra_files) == 0

    def test_capture_with_script(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            source = Path(tmpdir)
            (source / "SKILL.md").write_text("---\nname: test\n---\n# Test")
            scripts_dir = source / "scripts"
            scripts_dir.mkdir()
            script = scripts_dir / "run.sh"
            script.write_text("#!/bin/sh\necho hello")
            os.chmod(script, 0o755)

            snapshot = capture_directory(source)

            assert len(snapshot.extra_files) == 1
            assert snapshot.extra_files[0].path == "scripts/run.sh"
            assert snapshot.extra_files[0].executable is True

    def test_capture_excludes_git(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            source = Path(tmpdir)
            (source / "SKILL.md").write_text("---\nname: test\n---\n# Test")
            git_dir = source / ".git"
            git_dir.mkdir()
            (git_dir / "config").write_text("git config")

            snapshot = capture_directory(source)

            assert len(snapshot.extra_files) == 0

    def test_capture_excludes_venv(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            source = Path(tmpdir)
            (source / "SKILL.md").write_text("---\nname: test\n---\n# Test")
            venv_dir = source / ".venv"
            venv_dir.mkdir()
            (venv_dir / "pyvenv.cfg").write_text("config")

            snapshot = capture_directory(source)

            assert len(snapshot.extra_files) == 0
            assert any(".venv" in p for p in snapshot.excluded_paths)

    def test_capture_missing_skill_md(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            source = Path(tmpdir)
            (source / "README.md").write_text("# Readme")

            with pytest.raises(DirectoryCaptureError, match=r"SKILL\.md"):
                capture_directory(source)

    def test_capture_binary_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            source = Path(tmpdir)
            (source / "SKILL.md").write_text("---\nname: test\n---\n# Test")
            assets_dir = source / "assets"
            assets_dir.mkdir()
            (assets_dir / "icon.bin").write_bytes(b"\x00\xff\x00\xff")

            snapshot = capture_directory(source)

            assert len(snapshot.extra_files) == 1
            assert snapshot.extra_files[0].is_binary is True

    def test_snapshot_to_extra_files(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            source = Path(tmpdir)
            (source / "SKILL.md").write_text("---\nname: test\n---\n# Test")
            scripts_dir = source / "scripts"
            scripts_dir.mkdir()
            (scripts_dir / "run.sh").write_text("echo hello")
            assets_dir = source / "assets"
            assets_dir.mkdir()
            (assets_dir / "icon.bin").write_bytes(b"\x00\xff")

            snapshot = capture_directory(source)
            extra_files = snapshot_to_extra_files(snapshot)

            assert len(extra_files) == 2

            # Find the script file
            script_file = next(f for f in extra_files if f["path"] == "scripts/run.sh")
            assert script_file["content"] == "echo hello"
            assert "encoding" not in script_file  # UTF-8, no encoding field

            # Find the binary file
            bin_file = next(f for f in extra_files if f["path"] == "assets/icon.bin")
            assert bin_file["encoding"] == "base64"

    def test_capture_warns_on_sensitive(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            source = Path(tmpdir)
            (source / "SKILL.md").write_text("---\nname: test\n---\n# Test")
            (source / "secrets.txt").write_text("password123")

            snapshot = capture_directory(source)

            assert len(snapshot.warnings) > 0
            assert any("sensitive" in w.lower() for w in snapshot.warnings)
