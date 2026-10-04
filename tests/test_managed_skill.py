# SPDX-FileCopyrightText: 2026 Kaushik <kaushikrjpm10@gmail.com>
# SPDX-License-Identifier: Apache-2.0

# SPDX-License-Identifier: Apache-2.0
"""Disposable filesystem tests for managed folder ownership and recovery."""

import base64
import hashlib
import json
import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest

from observal_cli import lockfile, managed_skill
from observal_cli.skill_folder import validate_bundle


def bundle(version, content=b"skill", script=b"\x00\xff"):
    def file(path, data, mode):
        return {
            "path": path,
            "content": base64.b64encode(data).decode(),
            "sha256": hashlib.sha256(data).hexdigest(),
            "size": len(data),
            "mode": mode,
            "version_id": version,
        }

    return validate_bundle(
        {
            "listing_id": "listing-id",
            "version_id": version,
            "digest": f"sha256:{version}",
            "skill_file_path": ".pi/skills/example/SKILL.md",
            "files": [
                file("SKILL.md", content, "0644"),
                file("scripts/run", script, "0755"),
                file("empty", b"", "0644"),
            ],
        }
    )


@pytest.fixture
def store(tmp_path, monkeypatch):
    config = tmp_path / "config"
    monkeypatch.setattr(lockfile, "CONFIG_DIR", config)
    monkeypatch.setattr(lockfile, "LOCKFILE_PATH", config / "lockfile.json")
    monkeypatch.setattr(lockfile, "_LOCKFILE_LOCK", config / "lockfile.lock")
    return tmp_path / "skills" / "example", tmp_path / "backups"


def install(b, target, root, *, check=False, old=None, fail=False):
    proof = managed_skill.receipt(b, target, "pi", "project", registry_url="https://example.org")

    def record(data):
        if fail:
            raise OSError("injected lock failure")
        entries = (
            data.setdefault("registries", {})
            .setdefault("https://example.org", {"harnesses": {}})["harnesses"]
            .setdefault("pi", {"standalone": []})["standalone"]
        )
        entry = next((item for item in entries if item.get("folder_receipt", {}).get("target") == str(target)), None)
        if entry is None:
            entry = {"id": b.listing_id, "type": "skill"}
            entries.append(entry)
        entry.update({"folder_receipt": proof, "version_id": b.version_id, "digest": b.digest})

    return managed_skill.transact(b, target, proof, record=record, old_bundle=old, backup_root=root, check=check)


def test_upgrade_preview_does_not_create_backup_or_lock_directories(store):
    target, root = store
    first = bundle("v1")
    preview = install(first, target, root, check=True)
    assert preview["action"] == "install"
    assert not root.exists()
    assert not target.parent.exists()
    assert not lockfile.LOCKFILE_PATH.parent.exists()


def test_initial_upgrade_preview_restore_and_prune(store):
    target, root = store
    one, two = bundle("v1"), bundle("v2", content=b"changed")
    assert install(one, target, root)["action"] == "install"
    assert oct(os.stat(target / "scripts/run").st_mode & 0o777) == "0o755"
    assert (target / "empty").read_bytes() == b""
    assert install(one, target, root)["action"] == "unchanged"
    preview = install(two, target, root, check=True)
    assert preview["from_version_id"] == "v1"
    assert (target / "SKILL.md").read_bytes() == b"skill"
    done = install(two, target, root)
    assert (target / "SKILL.md").read_bytes() == b"changed"
    assert managed_skill.backups_list(backup_root=root)[0]["id"] == done["backup_id"]
    restored = managed_skill.restore_backup(done["backup_id"], backup_root=root)
    assert (target / "SKILL.md").read_bytes() == b"skill"
    assert managed_skill.restore_backup(restored["current_backup"], backup_root=root, prune=True)


def test_interrupted_restore_preserves_both_versions_and_prior_receipt(store):
    target, root = store
    first, second = bundle("v1"), bundle("v2", content=b"changed")
    install(first, target, root)
    upgraded = install(second, target, root)
    original = root / upgraded["backup_id"] / "old"
    replacement = root / f"{hashlib.sha256(str(target).encode()).hexdigest()}-restore-crash"
    replacement.mkdir()
    old_proof = managed_skill.receipt(second, target, "pi", "project", registry_url="https://example.org")
    new_proof = managed_skill.receipt(first, target, "pi", "project", registry_url="https://example.org")
    (replacement / "marker.json").write_text(
        json.dumps(
            {
                "target": str(target),
                "old": old_proof,
                "new": new_proof,
                "restore_from": str(original),
            }
        )
    )
    target.rename(replacement / "old")
    original.rename(target)
    managed_skill.recover(target, backup_root=root)
    assert (target / "SKILL.md").read_bytes() == b"changed"
    assert (original / "SKILL.md").read_bytes() == b"skill"
    assert not (replacement / "marker.json").exists()


def test_restore_lock_failure_preserves_active_and_backup(store, monkeypatch):
    target, root = store
    install(bundle("v1"), target, root)
    done = install(bundle("v2", content=b"changed"), target, root)
    monkeypatch.setattr(lockfile, "update_lockfile", lambda _record: (_ for _ in ()).throw(OSError("read only")))
    with pytest.raises(managed_skill.ManagedSkillError, match="active installation preserved"):
        managed_skill.restore_backup(done["backup_id"], backup_root=root)
    assert (target / "SKILL.md").read_bytes() == b"changed"
    assert (root / done["backup_id"] / "old" / "SKILL.md").read_bytes() == b"skill"


def test_modified_tree_and_unowned_folder_refused(store):
    target, root = store
    one = bundle("v1")
    target.mkdir(parents=True)
    (target / "SKILL.md").write_bytes(b"skill")
    with pytest.raises(managed_skill.ManagedSkillError, match="no receipt"):
        install(one, target, root)
    import shutil

    shutil.rmtree(target)
    install(one, target, root)
    (target / "extra").write_bytes(b"unexpected")
    with pytest.raises(managed_skill.ManagedSkillError, match="Modified or unexpected"):
        install(bundle("v2"), target, root)


def test_restore_refuses_changed_active_tree(store):
    target, root = store
    install(bundle("v1"), target, root)
    done = install(bundle("v2"), target, root)
    (target / "extra").write_bytes(b"user change")
    with pytest.raises(managed_skill.ManagedSkillError, match="Modified or unexpected"):
        managed_skill.restore_backup(done["backup_id"], backup_root=root)
    assert (root / done["backup_id"] / "old" / "SKILL.md").read_bytes() == b"skill"


def test_lockfile_failure_after_atomic_replace_retains_complete_new_release(store, monkeypatch):
    target, root = store
    install(bundle("v1"), target, root)
    saved = lockfile._write_locked

    def fail_after_commit(data):
        saved(data)
        raise OSError("injected post-replace failure")

    monkeypatch.setattr(lockfile, "_write_locked", fail_after_commit)
    with pytest.raises(managed_skill.ManagedSkillError, match="New skill is complete and recorded"):
        install(bundle("v2", content=b"changed"), target, root)
    assert (target / "SKILL.md").read_bytes() == b"changed"
    entry = lockfile.read_lockfile()["registries"]["https://example.org"]["harnesses"]["pi"]["standalone"][0]
    assert entry["version_id"] == "v2"
    monkeypatch.setattr(lockfile, "_write_locked", saved)
    managed_skill.recover(target, backup_root=root)
    assert (target / "SKILL.md").read_bytes() == b"changed"
    assert managed_skill.backups_list(backup_root=root)


def test_initial_and_upgrade_record_failure_restore(store):
    target, root = store
    one = bundle("v1")
    with pytest.raises(managed_skill.ManagedSkillError, match="original installation preserved"):
        install(one, target, root, fail=True)
    assert not target.exists()
    install(one, target, root)
    with pytest.raises(managed_skill.ManagedSkillError, match="original installation preserved"):
        install(bundle("v2"), target, root, fail=True)
    assert (target / "SKILL.md").read_bytes() == b"skill"
    assert (
        lockfile.read_lockfile()["registries"]["https://example.org"]["harnesses"]["pi"]["standalone"][0]["version_id"]
        == "v1"
    )


def test_recover_interrupted_rename_and_refuse_changed_target(store):
    target, root = store
    one = bundle("v1")
    install(one, target, root)
    key = hashlib.sha256(str(target).encode()).hexdigest()
    folder = root / f"{key}-123"
    folder.mkdir()
    proof = managed_skill.receipt(one, target, "pi", "project", registry_url="https://example.org")
    (folder / "marker.json").write_text(json.dumps({"target": str(target), "old": proof, "new": proof}))
    target.rename(folder / "old")
    managed_skill.recover(target, backup_root=root)
    assert (target / "SKILL.md").read_bytes() == b"skill"
    assert not (folder / "marker.json").exists()
    (target / "extra").write_text("edited")
    with pytest.raises(managed_skill.ManagedSkillError):
        managed_skill.restore_backup("bad-id", backup_root=root)


def test_recover_unrecorded_initial_swap_without_claiming_managed_ownership(store):
    target, root = store
    first = bundle("v1")
    target.parent.mkdir(parents=True)
    root.mkdir()
    key = hashlib.sha256(str(target).encode()).hexdigest()
    folder = root / f"{key}-initial"
    folder.mkdir()
    proof = managed_skill.receipt(first, target, "pi", "project", registry_url="https://example.org")
    (folder / "marker.json").write_text(json.dumps({"target": str(target), "old": None, "new": proof}))
    from observal_cli.skill_folder import install_folder_bundle

    install_folder_bundle(first, folder / "staged")
    (folder / "staged").rename(target)
    managed_skill.recover(target, backup_root=root)
    assert not target.exists()
    assert not (folder / "marker.json").exists()
    assert lockfile.read_lockfile()["registries"] == {}


def test_recover_crash_after_new_tree_before_receipt_keeps_old_release(store):
    target, root = store
    old, new = bundle("v1"), bundle("v2", content=b"changed")
    install(old, target, root)
    old_proof = managed_skill.receipt(old, target, "pi", "project", registry_url="https://example.org")
    new_proof = managed_skill.receipt(new, target, "pi", "project", registry_url="https://example.org")
    key = hashlib.sha256(str(target).encode()).hexdigest()
    folder = root / f"{key}-crash"
    folder.mkdir()
    (folder / "marker.json").write_text(json.dumps({"target": str(target), "old": old_proof, "new": new_proof}))
    from observal_cli.skill_folder import install_folder_bundle

    install_folder_bundle(new, folder / "staged")
    target.rename(folder / "old")
    (folder / "staged").rename(target)
    managed_skill.recover(target, backup_root=root)
    assert (target / "SKILL.md").read_bytes() == b"skill"
    assert not (folder / "marker.json").exists()
    assert (
        lockfile.read_lockfile()["registries"]["https://example.org"]["harnesses"]["pi"]["standalone"][0]["version_id"]
        == "v1"
    )


def test_symlink_replacement_during_staging_cannot_redirect_swap(store, tmp_path, monkeypatch):
    target, root = store
    target.parent.mkdir(parents=True)
    foreign = tmp_path / "foreign"
    (foreign / target.name).mkdir(parents=True)
    sentinel = foreign / target.name / "SKILL.md"
    sentinel.write_text("not owned")
    moved = target.parent.with_name("old-skill-root")
    real_install = managed_skill.install_folder_bundle

    def replace_parent(bundle_value, stage):
        result = real_install(bundle_value, stage)
        target.parent.rename(moved)
        target.parent.symlink_to(foreign, target_is_directory=True)
        return result

    monkeypatch.setattr(managed_skill, "install_folder_bundle", replace_parent)
    with pytest.raises(managed_skill.ManagedSkillError):
        install(bundle("v1"), target, root)
    assert sentinel.read_text() == "not owned"
    assert not (moved / target.name).exists()
    assert not lockfile.LOCKFILE_PATH.exists()


def test_backup_root_in_another_project_skill_tree_is_refused(store, tmp_path):
    target, _root = store
    discovery = tmp_path / "other-project" / ".pi" / "skills" / "backups"
    proof = managed_skill.receipt(bundle("v1"), target, "pi", "project", registry_url="https://example.org")
    with pytest.raises(managed_skill.ManagedSkillError, match="discovery root"):
        managed_skill.transact(bundle("v1"), target, proof, record=lambda _data: None, backup_root=discovery)
    assert not discovery.exists()
    assert not target.exists()


def test_renamed_successor_refuses_second_live_folder_and_keeps_prior_receipt(store):
    target, root = store
    one = bundle("v1")
    install(one, target, root)
    renamed = replace(bundle("v2"), folder_name="renamed", skill_file_path=".pi/skills/renamed/SKILL.md")
    other = target.parent / "renamed"
    proof = managed_skill.receipt(renamed, other, "pi", "project", registry_url="https://example.org")
    with pytest.raises(managed_skill.ManagedSkillError, match="different folder name"):
        managed_skill.transact(renamed, other, proof, record=lambda _data: None, backup_root=root)
    assert (target / "SKILL.md").read_bytes() == b"skill"
    assert not other.exists()


def test_symlink_target_refused(store):
    target, root = store
    target.parent.mkdir(parents=True)
    target.symlink_to(target.parent)
    with pytest.raises(managed_skill.ManagedSkillError, match="Symlink"):
        install(bundle("v1"), target, root)


def test_old_reader_v2_ignores_optional_receipt(store):
    target, root = store
    install(bundle("v1"), target, root)
    data = lockfile.read_lockfile()
    assert data["lock_version"] == 2
    entry = data["registries"]["https://example.org"]["harnesses"]["pi"]["standalone"][0]
    assert "folder_receipt" in entry
    # Legacy queries only consume known entry fields and do not rewrite receipts.
    assert entry["id"] == "listing-id"
    assert lockfile.read_lockfile() == data


def test_wide_existing_lockfile_is_narrowed(store):
    target, root = store
    install(bundle("v1"), target, root)
    os.chmod(lockfile.LOCKFILE_PATH, 0o666)
    install(bundle("v2"), target, root)
    assert lockfile.LOCKFILE_PATH.stat().st_mode & 0o777 == 0o600


def test_different_targets_do_not_lose_receipts(store):
    target, root = store
    second = target.parent / "second"
    b = bundle("v1")
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(install, b, place, root) for place in (target, second)]
        for future in futures:
            future.result()
    # Both records are serialized by the lockfile transaction, even when
    # callers hold different per-target locks.
    data = lockfile.read_lockfile()
    entries = data["registries"]["https://example.org"]["harnesses"]["pi"]["standalone"]
    assert {e["folder_receipt"]["target"] for e in entries} == {str(target), str(second)}
