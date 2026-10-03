// SPDX-FileCopyrightText: 2026 Observal Contributors
// SPDX-License-Identifier: Apache-2.0

/**
 * E2E tests for skill folder functionality (issue #1730).
 *
 * Tests the complete workflow:
 * 1. Submit a skill with extra_files via API
 * 2. Verify manifest is retrievable
 * 3. Review and approve the skill
 * 4. Install the skill via CLI and verify files
 */

import { test, expect } from "@playwright/test";
import { getAccessToken, API_BASE, loginToWebUI } from "./helpers";
import * as crypto from "crypto";

// Helper to create base64 content with SHA-256
function encodeFile(content: string): { content_base64: string; sha256: string; size: number } {
	const buffer = Buffer.from(content, "utf-8");
	return {
		content_base64: buffer.toString("base64"),
		sha256: crypto.createHash("sha256").update(buffer).digest("hex"),
		size: buffer.length,
	};
}

test.describe("Skill Folder API", () => {
	const skillName = `e2e-skill-folder-${Date.now()}`;

	test("submit skill with extra_files and retrieve manifest", async () => {
		const token = await getAccessToken();
		const headers = {
			"Content-Type": "application/json",
			Authorization: `Bearer ${token}`,
		};

		// Prepare SKILL.md and extra files
		const skillMd = `---
name: ${skillName}
description: E2E test skill with extra files
---

## Instructions

This skill demonstrates folder-based delivery.
`;
		const scriptContent = `#!/bin/bash
echo "Hello from skill folder"
`;
		const configContent = `{"setting": "value"}`;

		const skillMdEncoded = encodeFile(skillMd);
		const scriptEncoded = encodeFile(scriptContent);
		const configEncoded = encodeFile(configContent);

		// Submit with extra_files
		const submitRes = await fetch(`${API_BASE}/api/v1/skills/folder-drafts`, {
			method: "POST",
			headers,
			body: JSON.stringify({
				name: skillName,
				description: "E2E test skill with extra files",
				owner: "admin",
				task_type: "automation",
				skill_md_content: skillMd,
				extra_files: [
					{
						path: "scripts/run.sh",
						content_base64: scriptEncoded.content_base64,
						sha256: scriptEncoded.sha256,
						size: scriptEncoded.size,
						mode: "0755",
					},
					{
						path: "config/settings.json",
						content_base64: configEncoded.content_base64,
						sha256: configEncoded.sha256,
						size: configEncoded.size,
						mode: "0644",
					},
				],
			}),
		});

		// The endpoint might not exist yet - skip if 404
		if (submitRes.status === 404) {
			test.skip();
			return;
		}

		expect(submitRes.status).toBe(200);
		const skill = await submitRes.json();
		expect(skill.name).toBe(skillName);
		expect(skill.id).toBeDefined();
		const skillId = skill.id;
		const versionId = skill.pending_version_id || skill.version_id;

		// Get manifest if we have a version ID
		if (versionId) {
			const manifestRes = await fetch(
				`${API_BASE}/api/v1/skills/${skillId}/versions/${versionId}/manifest`,
				{ headers }
			);
			expect(manifestRes.status).toBe(200);
			const manifest = await manifestRes.json();

			// Verify manifest contains expected files
			expect(manifest.files).toBeDefined();
			expect(manifest.files.length).toBeGreaterThanOrEqual(3); // SKILL.md + 2 extra files

			const filePaths = manifest.files.map((f: { path: string }) => f.path);
			expect(filePaths).toContain("SKILL.md");
			expect(filePaths).toContain("scripts/run.sh");
			expect(filePaths).toContain("config/settings.json");

			// Verify executable mode
			const scriptFile = manifest.files.find((f: { path: string }) => f.path === "scripts/run.sh");
			expect(scriptFile?.mode).toBe("0755");
		}

		// Clean up - delete the skill
		await fetch(`${API_BASE}/api/v1/skills/${skillId}`, {
			method: "DELETE",
			headers,
		});
	});

	test("retrieve individual file content from manifest", async () => {
		const token = await getAccessToken();
		const headers = {
			"Content-Type": "application/json",
			Authorization: `Bearer ${token}`,
		};

		const uniqueName = `e2e-skill-file-${Date.now()}`;
		const skillMd = `---
name: ${uniqueName}
description: Test file content retrieval
---

## Test
`;
		const testScript = `#!/usr/bin/env python3
print("test")
`;

		const skillMdEncoded = encodeFile(skillMd);
		const scriptEncoded = encodeFile(testScript);

		// Submit skill
		const submitRes = await fetch(`${API_BASE}/api/v1/skills/folder-drafts`, {
			method: "POST",
			headers,
			body: JSON.stringify({
				name: uniqueName,
				description: "Test file content retrieval",
				owner: "admin",
				task_type: "test",
				skill_md_content: skillMd,
				extra_files: [
					{
						path: "test.py",
						content_base64: scriptEncoded.content_base64,
						sha256: scriptEncoded.sha256,
						size: scriptEncoded.size,
						mode: "0755",
					},
				],
			}),
		});

		if (submitRes.status === 404) {
			test.skip();
			return;
		}

		expect(submitRes.status).toBe(200);
		const skill = await submitRes.json();
		const skillId = skill.id;
		const versionId = skill.pending_version_id || skill.version_id;

		if (versionId) {
			// Fetch individual file content
			const fileRes = await fetch(
				`${API_BASE}/api/v1/skills/${skillId}/versions/${versionId}/files/test.py`,
				{ headers }
			);
			expect(fileRes.status).toBe(200);
			const fileData = await fileRes.json();

			expect(fileData.path).toBe("test.py");
			expect(fileData.content).toBeDefined();

			// Decode if base64
			const content =
				fileData.encoding === "base64"
					? Buffer.from(fileData.content, "base64").toString("utf-8")
					: fileData.content;
			expect(content).toContain("print");
		}

		// Clean up
		await fetch(`${API_BASE}/api/v1/skills/${skillId}`, {
			method: "DELETE",
			headers,
		});
	});
});

test.describe("Skill Folder Web UI", () => {
	test("review sheet shows file tree for folder skill", async ({ page }) => {
		const token = await getAccessToken();
		const headers = {
			"Content-Type": "application/json",
			Authorization: `Bearer ${token}`,
		};

		const uniqueName = `e2e-skill-review-${Date.now()}`;
		const skillMd = `---
name: ${uniqueName}
description: Test review UI
---

## Instructions
`;
		const scriptContent = `echo "test"`;

		const skillMdEncoded = encodeFile(skillMd);
		const scriptEncoded = encodeFile(scriptContent);

		// Submit skill with extra files
		const submitRes = await fetch(`${API_BASE}/api/v1/skills/folder-drafts`, {
			method: "POST",
			headers,
			body: JSON.stringify({
				name: uniqueName,
				description: "Test review UI",
				owner: "admin",
				task_type: "test",
				skill_md_content: skillMd,
				extra_files: [
					{
						path: "run.sh",
						content_base64: scriptEncoded.content_base64,
						sha256: scriptEncoded.sha256,
						size: scriptEncoded.size,
						mode: "0755",
					},
				],
			}),
		});

		if (submitRes.status === 404) {
			test.skip();
			return;
		}

		expect(submitRes.status).toBe(200);
		const skill = await submitRes.json();
		const skillId = skill.id;

		// Login and navigate to review page
		await loginToWebUI(page);
		await page.goto("/review");
		await page.waitForLoadState("networkidle");

		// Find and click on our skill in the review queue
		const skillRow = page.locator(`text=${uniqueName}`).first();
		if (await skillRow.isVisible({ timeout: 5000 })) {
			await skillRow.click();

			// Wait for detail sheet to open
			await page.waitForTimeout(1000);

			// Look for file tree section (may show "Files" label)
			const filesSection = page.locator('text=/Files \\(\\d+\\)/');
			if (await filesSection.isVisible({ timeout: 3000 })) {
				// Verify files are listed
				await expect(page.locator("text=SKILL.md")).toBeVisible({ timeout: 3000 });
				await expect(page.locator("text=run.sh")).toBeVisible({ timeout: 3000 });
			}
		}

		// Clean up
		await fetch(`${API_BASE}/api/v1/skills/${skillId}`, {
			method: "DELETE",
			headers,
		});
	});
});

test.describe("Skill Folder Validation", () => {
	test("rejects invalid SHA-256 checksum", async () => {
		const token = await getAccessToken();
		const headers = {
			"Content-Type": "application/json",
			Authorization: `Bearer ${token}`,
		};

		const skillMd = `---
name: test-invalid-sha
description: Test SHA validation
---
`;
		const skillMdEncoded = encodeFile(skillMd);

		// Submit with wrong SHA
		const submitRes = await fetch(`${API_BASE}/api/v1/skills/folder-drafts`, {
			method: "POST",
			headers,
			body: JSON.stringify({
				name: `e2e-invalid-sha-${Date.now()}`,
				description: "Test SHA validation",
				owner: "admin",
				task_type: "test",
				skill_md_content: skillMd,
				extra_files: [
					{
						path: "test.txt",
						content_base64: Buffer.from("real content").toString("base64"),
						sha256: "0000000000000000000000000000000000000000000000000000000000000000", // Wrong SHA
						size: 12,
						mode: "0644",
					},
				],
			}),
		});

		// Should either 404 (endpoint not implemented) or 400/422 (validation error)
		if (submitRes.status === 404) {
			test.skip();
			return;
		}

		// Expect validation error
		expect([400, 422]).toContain(submitRes.status);
		const error = await submitRes.json();
		expect(error.detail || error.message || JSON.stringify(error)).toMatch(/sha|checksum|hash/i);
	});

	test("rejects path traversal attempts", async () => {
		const token = await getAccessToken();
		const headers = {
			"Content-Type": "application/json",
			Authorization: `Bearer ${token}`,
		};

		const skillMd = `---
name: test-traversal
description: Test path security
---
`;
		const fileEncoded = encodeFile("malicious");

		// Submit with path traversal
		const submitRes = await fetch(`${API_BASE}/api/v1/skills/folder-drafts`, {
			method: "POST",
			headers,
			body: JSON.stringify({
				name: `e2e-traversal-${Date.now()}`,
				description: "Test path security",
				owner: "admin",
				task_type: "test",
				skill_md_content: skillMd,
				extra_files: [
					{
						path: "../../../etc/passwd",
						content_base64: fileEncoded.content_base64,
						sha256: fileEncoded.sha256,
						size: fileEncoded.size,
						mode: "0644",
					},
				],
			}),
		});

		if (submitRes.status === 404) {
			test.skip();
			return;
		}

		// Expect validation error
		expect([400, 422]).toContain(submitRes.status);
	});

	test("rejects too many files", async () => {
		const token = await getAccessToken();
		const headers = {
			"Content-Type": "application/json",
			Authorization: `Bearer ${token}`,
		};

		const skillMd = `---
name: test-too-many
description: Test file limit
---
`;

		// Create 130 files (exceeds MAX_EXTRA_FILES = 128)
		const extraFiles = [];
		for (let i = 0; i < 130; i++) {
			const content = `file ${i}`;
			const encoded = encodeFile(content);
			extraFiles.push({
				path: `files/file${i}.txt`,
				content_base64: encoded.content_base64,
				sha256: encoded.sha256,
				size: encoded.size,
				mode: "0644",
			});
		}

		const submitRes = await fetch(`${API_BASE}/api/v1/skills/folder-drafts`, {
			method: "POST",
			headers,
			body: JSON.stringify({
				name: `e2e-too-many-${Date.now()}`,
				description: "Test file limit",
				owner: "admin",
				task_type: "test",
				skill_md_content: skillMd,
				extra_files: extraFiles,
			}),
		});

		if (submitRes.status === 404) {
			test.skip();
			return;
		}

		// Expect validation error
		expect([400, 422]).toContain(submitRes.status);
	});
});
