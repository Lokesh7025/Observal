<!-- SPDX-FileCopyrightText: 2026 Kaushik <kaushikrjpm10@gmail.com> -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

# #1730: complete registry-direct skill folders — implementation handoff

This draft PR is the **integration branch for the whole feature**, not a request to merge a server-only release. The server contract is implemented; the CLI installer/authoring and web author/reviewer are not. Keep `registry.skill_folder_delivery_enabled` **false** and the PR **draft** until the end-to-end acceptance below is complete. A response containing a folder is not an installed skill.

## Implemented contract

- Persist bounded, canonical file trees on immutable skill versions, including text/binary resources and executable modes. Saved candidate drafts support whole-tree replace, per-file edits, rebase, submit, withdraw and exact-version review. Old resource-less skills and `git_fetch` retain their existing behavior.
- Review and delivery are tied to a reviewed version UUID and observed content revision. Stale, missing or changed review bindings fail closed. Selected agent versions carry pinned skill UUIDs and `observal-content-v2` digests. Legacy clients cannot receive a partial `SKILL.md` in place of a required whole tree.
- Resource-bearing standalone and pinned-agent delivery require the feature setting **and** `supported_features: ["skill_extra_files_v1"]`. Skill-capable harnesses receive a complete bundle or HTTP 409. The setting stays off until safe clients and a homogeneous server fleet are ready; older processes are not made safe by this setting.

**Executable examples, not hand-maintained pseudo-schemas:** `tests/fixtures/skill_folder_contract.json` covers draft, edit, rebase, submit and exact reviewer requests. `tests/fixtures/skill_folder_install_contract.json` covers standalone and agent install requests/responses, binary and executable bytes, paths, version IDs, hashes, pinned lock and 409 refusals. The tests in `tests/test_skill_folder_contract.py` and `tests/test_skill_install_contract_fixture.py` validate these fixtures against the server. Consult the actual schemas and routes if any example or API behavior changes.

| Journey | Relevant API |
|---|---|
| Author a folder or fork an approved base | `POST /api/v1/skills/folder-drafts`, `POST /api/v1/skills/{listing_id}/drafts` |
| Edit a candidate | `PUT`/`PATCH /api/v1/skills/{listing_id}/versions/{version_id}/files`, `PUT .../draft`, `POST .../rebase`, `POST .../submit` |
| Inspect exact files | `GET /api/v1/skills/{listing_id}/versions/{version_id}/manifest`, `GET .../files/{file_path}` |
| Review exact bytes | `GET /api/v1/review`, `GET /api/v1/review/skills/{listing_id}/versions/{version_id}`, `POST .../decision` with `observed_revision` |
| Request a complete installation | `POST /api/v1/skills/{listing_id}/install` (`bundle`) or `POST /api/v1/agents/{agent_id}/install` (`skill_bundles` and pinned `lock`) |

For example payloads and response fields use the fixtures rather than constructing requests from the abbreviated route table. An approved version is immutable; editing pending content requires withdrawal first. On an observed-revision mismatch or another 409, refresh the manifest/review target instead of retrying with unverified bytes.

## Remaining implementation work

1. **CLI authoring.** Read a local skill *folder*, preserve relative layout, UTF-8 and binary bytes, and executable modes; submit/create candidate/edit/withdraw/rebase/publish via the version-bound APIs. Support a complete tree, not just the existing one-script `--script` path. Update bundled `observal_cli/skills/` command instructions whenever CLI command syntax changes. Never infer reviewed content from the listing's current pointer.
2. **CLI installation (standalone, pinned agents and delegation).** Negotiate `skill_extra_files_v1`; decode and verify every file's base64, SHA-256, size, canonical relative path, mode, selected version ID and pinned v2 digest/lock **before** touching the destination. Enforce each harness's supported skill paths via its adapter and the registry, not a new harness if/elif chain. Reject symlinks, escapes, collisions, duplicate/case-conflicting paths and unsupported scope or modes. Stage a complete private tree, handle existing installs/adoption/backup deliberately, replace the tree as a unit, and update lock state only after a successful install. A failed required install must not launch a delegated child or leave a partially activated skill; exercise interrupted/rollback behavior on supported platforms. Do not silently fall back to a lone `SKILL.md` on HTTP 409.
3. **Web author and reviewer.** Add a full-folder editor (including binary/mode affordances, path validation and stale/rebase actions). Make the queue and detail page identify a **version UUID/review key**, not merely a listing; inspect manifest declarations and lazy-load authorized file contents before deciding with the observed revision. Handle private/team visibility and public re-review, 404/409, withdrawn versions and concurrent edits. Test keyboard, narrow and desktop layouts, light/dark themes and accessibility; attach screenshots of affected screens per `AI_POLICY.md`.
4. **Joint acceptance.** On a disposable compatible stack with the setting deliberately enabled, cover create → review of exact bytes → standalone and pinned-agent HTTP responses → CLI disk installation → harness loading → replacement/version change → rollback and delegation failure refusal. Include binary files, multiple scripts and templates, 60-file limits, executable mode, stale review, unauthenticated/old clients, unsupported harnesses, traversal/symlink attempts and malicious/corrupt payload refusals. Verify a normal legacy resource-less install still works. Re-run complete Python, isolated PostgreSQL, web and browser suites. Do not use the live old-image/revision-027 stack as a test deployment.
5. **Release gate.** Follow `docs/skill-folder-rollout.md` for backup, homogeneous maintenance-window migration and explicit settings-cache verification. The historical real-027 snapshot was checked only through migration 036, not the latest head; the packaged latest-head and branch-stamp tests use disposable PostgreSQL. Independently review the integrated diff, verify deployment/older-binary recovery with a tested DB restore, and obtain human approval before enabling folder delivery, making the PR ready or closing #1730.

The existing PR CI and server contract checks show a **gated development starting point**, not a safe complete feature or production rollout. Keep this tracked guide free of credentials, database dumps and the private local handoff notes.
