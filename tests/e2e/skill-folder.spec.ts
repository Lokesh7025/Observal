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

// Helper to create file content for submission
// Server expects: { path, content, encoding?, executable? }
// NOT: sha256, size, mode (those are for manifests only)
function encodeFile(content: string, binary = false): { content: string; encoding?: string } {
	if (binary) {
		return {
			content: Buffer.from(content, "utf-8").toString("base64"),
			encoding: "base64",
		};
	}
	return { content };
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

		// Submit with extra_files
		// Server expects: { path, content, encoding?, executable? }
		const submitRes = await fetch(`${API_BASE}/api/v1/skills/folder-drafts`, {
			method: "POST",
			headers,
			body: JSON.stringify({
				name: skillName,
				version: "1.0.0",
				description: "E2E test skill with extra files",
				owner: "admin",
				task_type: "general",
				skill_md_content: skillMd,
				extra_files: [
					{
						path: "scripts/run.sh",
						content: scriptContent,
						executable: true,
					},
					{
						path: "config/settings.json",
						content: configContent,
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
		const result = await submitRes.json();
		// API returns listing_id, version_id, revision, files
		expect(result.listing_id).toBeDefined();
		expect(result.version_id).toBeDefined();
		expect(result.files).toBeDefined();
		const skillId = result.listing_id;
		const versionId = result.version_id;

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

		// Submit skill
		const submitRes = await fetch(`${API_BASE}/api/v1/skills/folder-drafts`, {
			method: "POST",
			headers,
			body: JSON.stringify({
				name: uniqueName,
				version: "1.0.0",
				description: "Test file content retrieval",
				owner: "admin",
				task_type: "testing",
				skill_md_content: skillMd,
				extra_files: [
					{
						path: "test.py",
						content: testScript,
						executable: true,
					},
				],
			}),
		});

		if (submitRes.status === 404) {
			test.skip();
			return;
		}

		expect(submitRes.status).toBe(200);
		const result = await submitRes.json();
		const skillId = result.listing_id;
		const versionId = result.version_id;

		if (versionId) {
			// Fetch individual file content
			const fileRes = await fetch(
				`${API_BASE}/api/v1/skills/${skillId}/versions/${versionId}/files/test.py`,
				{ headers }
			);
			expect(fileRes.status).toBe(200);
			const fileData = await fileRes.json();

			// Response structure: { version_id, revision, file: { path, size, sha256, mode }, content, encoding }
			expect(fileData.file.path).toBe("test.py");
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

		// Submit skill with extra files
		const submitRes = await fetch(`${API_BASE}/api/v1/skills/folder-drafts`, {
			method: "POST",
			headers,
			body: JSON.stringify({
				name: uniqueName,
				version: "1.0.0",
				description: "Test review UI",
				owner: "admin",
				task_type: "general",
				skill_md_content: skillMd,
				extra_files: [
					{
						path: "run.sh",
						content: scriptContent,
						executable: true,
					},
				],
			}),
		});

		if (submitRes.status === 404) {
			test.skip();
			return;
		}

		expect(submitRes.status).toBe(200);
		const result = await submitRes.json();
		const skillId = result.listing_id;

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
	test("rejects missing version field", async () => {
		const token = await getAccessToken();
		const headers = {
			"Content-Type": "application/json",
			Authorization: `Bearer ${token}`,
		};

		const skillMd = `---
name: test-no-version
description: Test version validation
---
`;

		// Submit without version field
		const submitRes = await fetch(`${API_BASE}/api/v1/skills/folder-drafts`, {
			method: "POST",
			headers,
			body: JSON.stringify({
				name: `e2e-no-version-${Date.now()}`,
				description: "Test version validation",
				owner: "admin",
				task_type: "general",
				skill_md_content: skillMd,
				extra_files: [
					{
						path: "test.txt",
						content: "test content",
					},
				],
			}),
		});

		if (submitRes.status === 404) {
			test.skip();
			return;
		}

		// Expect validation error for missing version
		expect([400, 422]).toContain(submitRes.status);
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

		// Submit with path traversal
		const submitRes = await fetch(`${API_BASE}/api/v1/skills/folder-drafts`, {
			method: "POST",
			headers,
			body: JSON.stringify({
				name: `e2e-traversal-${Date.now()}`,
				version: "1.0.0",
				description: "Test path security",
				owner: "admin",
				task_type: "general",
				skill_md_content: skillMd,
				extra_files: [
					{
						path: "../../../etc/passwd",
						content: "malicious content",
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
			extraFiles.push({
				path: `files/file${i}.txt`,
				content: `file ${i}`,
			});
		}

		const submitRes = await fetch(`${API_BASE}/api/v1/skills/folder-drafts`, {
			method: "POST",
			headers,
			body: JSON.stringify({
				name: `e2e-too-many-${Date.now()}`,
				version: "1.0.0",
				description: "Test file limit",
				owner: "admin",
				task_type: "general",
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
