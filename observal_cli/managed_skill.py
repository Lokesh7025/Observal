# SPDX-FileCopyrightText: 2026 Kaushik <kaushikrjpm10@gmail.com>
# SPDX-License-Identifier: Apache-2.0

# SPDX-License-Identifier: Apache-2.0
"""Machine-local, verified folder transactions. Agent pulls may reuse this API.

No network calls are made here. The caller must supply reviewed exact-version
bundles (including the old release for receipt-less adoption).
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
import uuid
from contextlib import nullcontext
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

from observal_cli import lockfile
from observal_cli.skill_folder import ValidatedBundle, detect_destination_collisions, install_folder_bundle


class ManagedSkillError(RuntimeError):
    pass


def receipt(
    bundle: ValidatedBundle,
    target: Path,
    harness: str,
    scope: str,
    *,
    registry_url: str,
    source: str = "standalone",
    agent_id: str | None = None,
    agent_version: str | None = None,
) -> dict:
    """Build a v1 provenance record; no file contents or credentials."""
    if source not in {"standalone", "agent"} or (source == "agent" and not (agent_id and agent_version)):
        raise ManagedSkillError("An Agent receipt needs an exact Agent ID and pinned version")
    return {
        "receipt_version": 1,
        "registry_url": lockfile.normalize_server_url(registry_url),
        "listing_id": bundle.listing_id,
        "version_id": bundle.version_id,
        "digest": bundle.digest,
        "harness": harness,
        "scope": scope,
        "target": str(target.absolute()),
        "source": source,
        "agent_id": agent_id,
        "agent_version": agent_version,
        "files": [
            {"path": f.path, "sha256": f.sha256, "size": f.size, "mode": f"{f.mode:04o}"}
            for f in sorted(bundle.files, key=lambda f: f.path)
        ],
    }


def verify_tree(target: Path, proof: dict) -> None:
    """Refuse symlinks, special files, changed permissions and *all* extra files."""
    if not isinstance(proof, dict) or not isinstance(proof.get("files"), list):
        raise ManagedSkillError("Invalid folder receipt")
    expected = {f["path"]: f for f in proof["files"]}
    if not target.is_dir() or target.is_symlink():
        raise ManagedSkillError(f"Missing or symlinked managed folder: {target}")
    actual = set()
    for path in target.rglob("*"):
        if path.is_symlink() or not (path.is_file() or path.is_dir()):
            raise ManagedSkillError(f"Symlink or special file in managed folder: {path}")
        if path.is_dir():
            continue
        relative = path.relative_to(target).as_posix()
        actual.add(relative)
        record = expected.get(relative)
        if (
            record is None
            or path.stat().st_size != record["size"]
            or stat.S_IMODE(path.stat().st_mode) != int(record["mode"], 8)
            or hashlib.sha256(path.read_bytes()).hexdigest() != record["sha256"]
        ):
            raise ManagedSkillError(f"Modified or unexpected skill file: {path}")
    if actual != expected.keys():
        raise ManagedSkillError(f"Missing skill files: {sorted(expected.keys() - actual)}")


def _safe_parents(path: Path) -> None:
    for part in (path, *path.parents):
        if part.is_symlink():
            raise ManagedSkillError(f"Symlink in destination or backup path: {part}")


def _backup_root(target: Path, root: Path | None, *, create: bool = True) -> Path:
    root = (root or lockfile.CONFIG_DIR / "backups" / "skills").expanduser().absolute()
    _safe_parents(root)
    _safe_parents(target)
    from observal_shared.harness_registry import HARNESS_REGISTRY

    # All global discovery roots and all project discovery roots for this worktree.
    for spec in HARNESS_REGISTRY.values():
        for template in spec.get("skills", {}).values():
            if not template:
                continue
            discovery = Path(template.format(name="__observal_probe__")).expanduser()
            if not discovery.is_absolute():
                relative_parts = discovery.parent.parent.parts
                root_parts = root.parts
                if any(
                    root_parts[index : index + len(relative_parts)] == relative_parts
                    for index in range(len(root_parts) - len(relative_parts) + 1)
                ):
                    raise ManagedSkillError(f"Backup root inside a project skill discovery root: {root}")
                discovery = Path.cwd() / discovery
            discovery = discovery.parent.parent.absolute()
            if root == discovery or root.is_relative_to(discovery):
                raise ManagedSkillError(f"Backup root inside a skill discovery root: {root}")
    if create:
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(root, 0o700)
        target.parent.mkdir(parents=True, exist_ok=True)
    root_ancestor = next((part for part in (root, *root.parents) if part.exists()), None)
    target_ancestor = next((part for part in (target.parent, *target.parent.parents) if part.exists()), None)
    if root_ancestor is None or target_ancestor is None or root_ancestor.stat().st_dev != target_ancestor.stat().st_dev:
        raise ManagedSkillError("Backup root is on another filesystem; provide --backup-root on the target filesystem")
    try:
        git = subprocess.run(
            ["git", "-C", str(root_ancestor), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    except FileNotFoundError:
        # Git is optional for registry-direct installs. Without it, do not
        # assume that a backup under a discoverable worktree is ignored.
        if any(
            (parent / ".git").exists() or (parent / ".git").is_symlink()
            for parent in (root_ancestor, *root_ancestor.parents)
        ):
            raise ManagedSkillError("Cannot verify that the backup root is Git-ignored without Git") from None
        return root
    except subprocess.TimeoutExpired as exc:
        raise ManagedSkillError("Could not determine whether the backup root is Git-tracked") from exc
    if git.returncode == 0:
        try:
            ignored = subprocess.run(
                ["git", "-C", str(root_ancestor), "check-ignore", "-q", str(root)], check=False, timeout=10
            )
        except subprocess.TimeoutExpired as exc:
            raise ManagedSkillError("Could not verify that the backup root is Git-ignored") from exc
        if ignored.returncode != 0:
            raise ManagedSkillError(f"Backup root in a Git worktree must be ignored by Git: {root}")
    return root


def _records(data: dict, target: Path) -> list[tuple[str, dict]]:
    matches = []
    for url, registry in data.get("registries", {}).items():
        for section in registry.get("harnesses", {}).values():
            for entry in section.get("standalone", []):
                proof = entry.get("folder_receipt")
                if proof and proof.get("target") == str(target):
                    matches.append((url, entry))
            for agent in section.get("agents", []):
                for component in agent.get("components", []):
                    proof = component.get("folder_receipt")
                    if proof and proof.get("target") == str(target):
                        matches.append((url, component))
    return matches


def _marker(root: Path, target: Path) -> list[Path]:
    key = hashlib.sha256(str(target).encode()).hexdigest()
    return list(root.glob(f"{key}-*/marker.json"))


def recover(target: Path, *, backup_root: Path | None = None) -> None:
    """On the next invocation restore a missing target, or refuse ambiguous state."""
    root = _backup_root(target, backup_root)
    for marker in _marker(root, target):
        state = json.loads(marker.read_text())
        backup = marker.parent / "old"
        if state.get("target") != str(target):
            raise ManagedSkillError(f"Invalid recovery marker: {marker}")
        if not target.exists() and not target.is_symlink() and backup.is_dir():
            verify_tree(backup, state["old"])
            backup.rename(target)
        if target.exists() and state.get("old"):
            try:
                verify_tree(target, state["old"])
            except ManagedSkillError as exc:
                # The lock may already have advanced before marker finalization.
                matches = _records(lockfile.read_lockfile(), target)
                if len(matches) > 1 or (
                    matches and matches[0][1].get("folder_receipt") not in (state["old"], state["new"])
                ):
                    raise ManagedSkillError(
                        f"Interrupted swap; inspect target and retained backup at {marker.parent}; no files overwritten"
                    ) from exc
                verify_tree(target, state["new"])
                if not matches or matches[0][1].get("folder_receipt") == state["old"]:
                    # A crash after the second rename but before recording the
                    # new receipt: restore the verified prior version. Never
                    # mark an unrecorded new directory as installed.
                    verify_tree(backup, state["old"])
                    restore_from = state.get("restore_from")
                    if restore_from:
                        original = Path(restore_from)
                        if (
                            original.name != "old"
                            or original.parent.parent != root
                            or original.exists()
                            or original.is_symlink()
                        ):
                            raise ManagedSkillError(f"Restore backup slot changed; inspect {marker}")
                        # Return the verified older copy to its original backup
                        # slot instead of deleting the only recovery copy.
                        target.rename(original)
                    else:
                        shutil.rmtree(target)
                    backup.rename(target)
                    marker.unlink()
                    continue
                if not (marker.parent / "backup.json").exists():
                    (marker.parent / "backup.json").write_text(json.dumps(state))
                    os.chmod(marker.parent / "backup.json", 0o600)
            marker.unlink()
        elif not target.exists():
            marker.unlink()  # interrupted initial install, before any installed folder
        else:
            matches = _records(lockfile.read_lockfile(), target)
            if len(matches) == 1 and matches[0][1].get("folder_receipt") == state["new"]:
                verify_tree(target, state["new"])
                marker.unlink()
            elif not matches:
                # Initial swap finished but receipt write did not. Only a byte-
                # for-byte complete new tree may be removed; changed files are
                # left untouched for the user to inspect.
                verify_tree(target, state["new"])
                shutil.rmtree(target)
                marker.unlink()
            else:
                raise ManagedSkillError(f"Interrupted initial install at {target}; inspect {marker} before retrying")


def transact(
    bundle: ValidatedBundle,
    target: Path,
    proof: dict,
    *,
    record: Callable[[dict], None],
    old_bundle: ValidatedBundle | None = None,
    backup_root: Path | None = None,
    check: bool = False,
    allow_agent_pin_change: bool = False,
) -> dict:
    """Verify and install one folder; record(data) mutates a v2 lockfile under its lock.

    The Agent caller can use the same preflight and swap, but must coordinate
    its other writes and component receipt mutation separately.
    """
    if os.name != "posix":
        raise ManagedSkillError("Managed folder swaps require POSIX filesystem semantics")
    target = target.expanduser().absolute()
    if proof != receipt(
        bundle,
        target,
        proof["harness"],
        proof["scope"],
        registry_url=proof["registry_url"],
        source=proof["source"],
        agent_id=proof.get("agent_id"),
        agent_version=proof.get("agent_version"),
    ):
        raise ManagedSkillError("Receipt does not match selected verified bundle")
    # Preview reads do not create directories, markers, or even lock files.
    # Its observation can become stale; a later actual install rechecks under
    # the per-target cross-process lock before writing anything.
    if not check:
        lockfile.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    key = hashlib.sha256(str(target).encode()).hexdigest()
    target_lock = nullcontext() if check else lockfile._exclusive_lock(lockfile.CONFIG_DIR / f"skill-{key}.lock")
    with target_lock:
        root = _backup_root(target, backup_root, create=not check)
        if check:
            if _marker(root, target):
                raise ManagedSkillError("Interrupted folder swap; recover before previewing another change")
        else:
            recover(target, backup_root=root)
        collisions = detect_destination_collisions(target, bundle)
        if any("bundled" in message or "case-insensitive" in message for message in collisions):
            raise ManagedSkillError("Reserved or colliding skill destination: " + "; ".join(collisions))
        if check:
            if lockfile.LOCKFILE_PATH.exists():
                data = json.loads(lockfile.LOCKFILE_PATH.read_text())
                if data.get("lock_version") != lockfile.LOCK_VERSION:
                    raise ManagedSkillError("Migrate the machine lockfile before previewing a folder upgrade")
            else:
                data = {"registries": {}}
        else:
            data = lockfile.read_lockfile()
        matches = _records(data, target)
        for url, registry in data.get("registries", {}).items():
            if url != proof["registry_url"]:
                continue
            section = registry.get("harnesses", {}).get(proof["harness"], {})
            for standalone in section.get("standalone", []):
                if standalone.get("type") != "skill" or standalone.get("id") != bundle.listing_id:
                    continue
                existing = standalone.get("folder_receipt")
                if existing and (
                    existing.get("scope") == proof["scope"]
                    and existing.get("source") == proof["source"]
                    and Path(existing.get("target", "")).parent == target.parent
                    and existing.get("target") != str(target)
                ):
                    raise ManagedSkillError("This listing already owns a different folder name in this skill root")
                if (
                    not existing
                    and proof["source"] == "standalone"
                    and standalone.get("scope") == proof["scope"]
                    and (standalone.get("directory") or None)
                    == (str(Path.cwd()) if proof["scope"] == "project" else None)
                    and standalone.get("local_name") not in (None, bundle.folder_name)
                ):
                    raise ManagedSkillError("Legacy installation uses another folder name; explicit migration required")
            for agent in section.get("agents", []):
                if agent.get("id") != proof.get("agent_id") or proof["source"] != "agent":
                    continue
                for component in agent.get("components", []):
                    existing = component.get("folder_receipt")
                    if (
                        component.get("type") == "skill"
                        and component.get("id") == bundle.listing_id
                        and existing
                        and existing.get("scope") == proof["scope"]
                        and Path(existing.get("target", "")).parent == target.parent
                        and existing.get("target") != str(target)
                    ):
                        raise ManagedSkillError("Pinned Agent listing already owns a different folder name here")
        if len(matches) > 1 or (
            matches
            and (
                matches[0][0] != proof["registry_url"]
                or matches[0][1].get("folder_receipt", {}).get("source") != proof["source"]
            )
        ):
            raise ManagedSkillError("Destination belongs to another registry or owner")
        previous = matches[0][1].get("folder_receipt") if matches else None
        if target.exists() or target.is_symlink():
            if target.is_symlink():
                raise ManagedSkillError("Symlink target cannot be managed")
            if previous is None:
                if old_bundle is None or old_bundle.listing_id != bundle.listing_id:
                    raise ManagedSkillError(
                        "Existing folder has no receipt; supply its exact reviewed prior version for verification"
                    )
                # Legacy adoption is authorized only by an unambiguous matching lock entry.
                url = proof["registry_url"]
                entries = [
                    e
                    for e in data.get("registries", {})
                    .get(url, {})
                    .get("harnesses", {})
                    .get(proof["harness"], {})
                    .get("standalone", [])
                    if e.get("type") == "skill"
                    and e.get("id") == bundle.listing_id
                    and e.get("version_id") == old_bundle.version_id
                    and e.get("digest") == old_bundle.digest
                    and e.get("scope") == proof["scope"]
                    and (e.get("directory") or None) == (str(Path.cwd()) if proof["scope"] == "project" else None)
                ]
                if len(entries) != 1:
                    raise ManagedSkillError("No unambiguous legacy exact-version lock entry; manual migration required")
                previous = receipt(old_bundle, target, proof["harness"], proof["scope"], registry_url=url)
            if (
                previous["target"] != str(target)
                or previous["listing_id"] != bundle.listing_id
                or previous["harness"] != proof["harness"]
                or previous["scope"] != proof["scope"]
                or previous["registry_url"] != proof["registry_url"]
                or previous["source"] != proof["source"]
                or previous.get("agent_id") != proof.get("agent_id")
                or (
                    previous.get("agent_version") != proof.get("agent_version")
                    and not (allow_agent_pin_change and proof["source"] == "agent")
                )
            ):
                raise ManagedSkillError("Installed provenance differs from requested owner, scope or listing")
            if (
                previous["version_id"] != bundle.version_id
                and old_bundle
                and (old_bundle.version_id != previous["version_id"] or old_bundle.digest != previous["digest"])
            ):
                raise ManagedSkillError("Previously reviewed version does not match receipt")
            verify_tree(target, previous)
            if previous["version_id"] == bundle.version_id:
                if previous == proof:
                    if not matches and not check:
                        # A receipt-less historic install was verified against
                        # the exact reviewed bytes. Adoption must actually
                        # persist provenance, not report a no-op with no lock.
                        lockfile.update_lockfile(record)
                        return {"action": "adopted", "target": str(target)}
                    return {"action": "unchanged", "target": str(target)}
                if not (
                    allow_agent_pin_change
                    and proof["source"] == "agent"
                    and {k: v for k, v in previous.items() if k != "agent_version"}
                    == {k: v for k, v in proof.items() if k != "agent_version"}
                ):
                    raise ManagedSkillError("Same version has different reviewed content")
                # Treat a deliberate Agent repin as a backed-up transaction too,
                # so a later Agent write can restore the previous ownership pin.
            if target.name != bundle.folder_name or (old_bundle and old_bundle.folder_name != bundle.folder_name):
                raise ManagedSkillError("Folder name changed; explicit rename/migration required")
        elif matches:
            raise ManagedSkillError("Managed folder missing; restore a backup before installing")
        elif collisions:
            raise ManagedSkillError("Destination conflicts with existing files: " + "; ".join(collisions))
        if check:
            return {
                "action": "upgrade" if previous else "install",
                "target": str(target),
                "from_version_id": previous["version_id"] if previous else None,
                "to_version_id": bundle.version_id,
                "backup_root": str(root),
                "added": sorted(
                    {f["path"] for f in proof["files"]}
                    - ({f["path"] for f in previous["files"]} if previous else set())
                ),
                "removed": sorted(
                    ({f["path"] for f in previous["files"]} if previous else set())
                    - {f["path"] for f in proof["files"]}
                ),
            }
        folder = root / f"{key}-{uuid.uuid4().hex}"
        stage = folder / "staged"
        folder.mkdir(mode=0o700)
        os.chmod(folder, 0o700)
        marker = folder / "marker.json"
        backup = folder / "old"
        old_version = matches[0][1].get("version") if matches else None
        parent_fd: int | None = None
        folder_fd: int | None = None

        def parent_stable() -> bool:
            if parent_fd is None or target.parent.is_symlink():
                return False
            try:
                current = target.parent.stat(follow_symlinks=False)
                pinned = os.fstat(parent_fd)
            except OSError:
                return False
            return (current.st_dev, current.st_ino) == (pinned.st_dev, pinned.st_ino)

        try:
            install_folder_bundle(bundle, stage)
            verify_tree(stage, proof)
            # Marker exists before any rename. Atomic replacement + fsync for crash visibility.
            marker.write_text(json.dumps({"target": str(target), "old": previous, "new": proof}))
            os.chmod(marker, 0o600)
            with marker.open("rb") as handle:
                os.fsync(handle.fileno())
            # Pin both parents before changing any active path; never follow a
            # substituted symlink through a path-based rename during the swap.
            parent_fd = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            folder_fd = os.open(folder, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            if not parent_stable():
                raise ManagedSkillError("Skill destination parent changed during staging; no files were swapped")
            if previous:
                verify_tree(target, previous)
                os.rename(target.name, "old", src_dir_fd=parent_fd, dst_dir_fd=folder_fd)
                # Another local writer may replace the path after the first
                # verification. Never record a release if the folder actually
                # moved into the backup differs from the reviewed old tree.
                verify_tree(backup, previous)
            os.rename("staged", target.name, src_dir_fd=folder_fd, dst_dir_fd=parent_fd)
            if not parent_stable():
                raise ManagedSkillError(f"Destination parent moved during swap; inspect recovery marker {marker}")
            if previous:
                (folder / "backup.json").write_text(
                    json.dumps(
                        {
                            "target": str(target),
                            "old": previous,
                            "new": proof,
                            "old_version": old_version,
                            "new_version": None,
                        }
                    )
                )
                os.chmod(folder / "backup.json", 0o600)
            lockfile.update_lockfile(record)
            try:
                marker.unlink()
            except OSError as exc:
                # The lock has advanced: never roll back a committed installation.
                raise ManagedSkillError(
                    f"New skill is complete and recorded; finalize marker at {marker}: {exc}"
                ) from exc
            return {
                "action": "upgrade" if previous else "install",
                "target": str(target),
                "backup": str(backup) if previous else None,
                "backup_id": folder.name,
            }
        except Exception as exc:
            if isinstance(exc, ManagedSkillError) and str(exc).startswith("New skill is complete and recorded"):
                raise
            if marker.exists() and target.is_dir() and not target.is_symlink():
                try:
                    observed = _records(lockfile.read_lockfile(), target)
                    if len(observed) == 1 and observed[0][1].get("folder_receipt") == proof:
                        verify_tree(target, proof)
                        raise ManagedSkillError(
                            f"New skill is complete and recorded; finalize recovery marker at {marker}: {exc}"
                        ) from exc
                except ManagedSkillError as committed:
                    if str(committed).startswith("New skill is complete and recorded"):
                        raise
                    raise ManagedSkillError(
                        f"Cannot establish lock state after swap at {target}; inspect retained backup {folder}"
                    ) from committed
                except (OSError, RuntimeError, ValueError) as uncertain:
                    raise ManagedSkillError(
                        f"Cannot read lock after swap at {target}; inspect retained backup {folder}"
                    ) from uncertain
            if parent_fd is not None and not parent_stable():
                raise ManagedSkillError(
                    f"Skill destination parent changed; inspect retained recovery marker {marker}"
                ) from exc
            if previous and backup.exists():
                try:
                    verify_tree(backup, previous)
                    if target.exists():
                        verify_tree(target, proof)
                        shutil.rmtree(target)
                    if parent_fd is not None and folder_fd is not None:
                        os.rename("old", target.name, src_dir_fd=folder_fd, dst_dir_fd=parent_fd)
                    else:
                        backup.rename(target)
                    marker.unlink(missing_ok=True)
                except Exception as recovery_error:
                    raise ManagedSkillError(
                        f"Swap failed; manual recovery needed at {folder}: {recovery_error}"
                    ) from exc
            elif target.exists() and not previous:
                try:
                    verify_tree(target, proof)
                    shutil.rmtree(target)
                    marker.unlink(missing_ok=True)
                except Exception as recovery_error:
                    raise ManagedSkillError(
                        f"Initial install not recorded; inspect {target} and {folder}: {recovery_error}"
                    ) from exc
            raise ManagedSkillError(f"Folder transaction failed; original installation preserved: {exc}") from exc
        finally:
            if folder_fd is not None:
                os.close(folder_fd)
            if parent_fd is not None:
                os.close(parent_fd)
            if stage.exists():
                shutil.rmtree(stage)


def backups_list(*, backup_root: Path | None = None) -> list[dict]:
    root = (backup_root or lockfile.CONFIG_DIR / "backups" / "skills").expanduser().absolute()
    if not root.exists():
        return []
    _safe_parents(root)
    result = []
    for folder in root.iterdir():
        if not folder.is_dir() or folder.is_symlink():
            continue
        marker = folder / "marker.json"
        backup = folder / "old"
        if backup.is_dir() and not marker.exists():
            info = json.loads((folder / "backup.json").read_text()) if (folder / "backup.json").exists() else None
            if info:
                result.append(
                    {
                        "id": folder.name,
                        "target": info["target"],
                        "version_id": info["old"]["version_id"],
                        "age_seconds": int(__import__("time").time() - backup.stat().st_mtime),
                        "size": sum(p.stat().st_size for p in backup.rglob("*") if p.is_file()),
                    }
                )
    return result


def restore_backup(backup_id: str, *, backup_root: Path | None = None, prune: bool = False) -> dict:
    root = (backup_root or lockfile.CONFIG_DIR / "backups" / "skills").expanduser().absolute()
    if not backup_id or not all(c in "0123456789abcdef-" for c in backup_id):
        raise ManagedSkillError("Invalid backup ID")
    folder = root / backup_id
    if folder.is_symlink() or not (folder / "backup.json").is_file():
        raise ManagedSkillError("Unknown or unfinished backup")
    state = json.loads((folder / "backup.json").read_text())
    target = Path(state["target"])
    key = hashlib.sha256(str(target).encode()).hexdigest()
    with lockfile._exclusive_lock(lockfile.CONFIG_DIR / f"skill-{key}.lock"):
        recover(target, backup_root=root)
        verify_tree(folder / "old", state["old"])
        verify_tree(target, state["new"])
        matches = _records(lockfile.read_lockfile(), target)
        if len(matches) != 1 or matches[0][1].get("folder_receipt") != state["new"]:
            raise ManagedSkillError("Active lock receipt changed; refusing backup operation")
        if prune:
            shutil.rmtree(folder)
            return {"pruned": backup_id}
        # Restoring consumes the old backup. Retain the current release as a new backup.
        replacement = root / f"{key}-{uuid.uuid4().hex}"
        replacement.mkdir(mode=0o700)
        (replacement / "marker.json").write_text(
            json.dumps(
                {
                    "target": str(target),
                    "old": state["new"],
                    "new": state["old"],
                    "restore_from": str(folder / "old"),
                }
            )
        )
        os.chmod(replacement / "marker.json", 0o600)
        committed = False
        try:
            target.rename(replacement / "old")
            (folder / "old").rename(target)

            def update(data: dict) -> None:
                records = _records(data, target)
                records[0][1]["folder_receipt"] = state["old"]
                records[0][1]["version_id"] = state["old"]["version_id"]
                records[0][1]["digest"] = state["old"]["digest"]
                if state.get("old_version"):
                    records[0][1]["version"] = state["old_version"]
                records[0][1].pop("requested_version", None)

            (replacement / "backup.json").write_text(
                json.dumps(
                    {
                        "target": str(target),
                        "old": state["new"],
                        "new": state["old"],
                        "old_version": matches[0][1].get("version"),
                        "new_version": state.get("old_version"),
                    }
                )
            )
            os.chmod(replacement / "backup.json", 0o600)
            lockfile.update_lockfile(update)
            committed = True
            (replacement / "marker.json").unlink()
            shutil.rmtree(folder)
            return {"restored": backup_id, "current_backup": replacement.name}
        except Exception as exc:
            if committed:
                raise ManagedSkillError(
                    f"Restored skill is complete and recorded; finalize backup at {replacement}: {exc}"
                ) from exc
            try:
                current = replacement / "old"
                original = folder / "old"
                if current.is_dir():
                    verify_tree(current, state["new"])
                    if target.exists():
                        verify_tree(target, state["old"])
                        target.rename(original)
                    if not target.exists():
                        current.rename(target)
                    (replacement / "marker.json").unlink(missing_ok=True)
            except (OSError, ManagedSkillError) as rollback_error:
                raise ManagedSkillError(
                    f"Restore interrupted; manual recovery at {replacement} and {folder}: {rollback_error}"
                ) from exc
            raise ManagedSkillError(f"Restore failed; active installation preserved: {exc}") from exc
