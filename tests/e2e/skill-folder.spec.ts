// SPDX-FileCopyrightText: 2026 Observal Contributors
// SPDX-FileCopyrightText: 2026 Kaushik <kaushikrjpm10@gmail.com>
// SPDX-License-Identifier: Apache-2.0

/** Browser and HTTP acceptance for the strict skill-folder draft contract. */
import { test, expect } from "@playwright/test";
import { getAccessToken, API_BASE, loginToWebUI } from "./helpers";
import { randomUUID } from "node:crypto";

function skillName(prefix: string) {
	return `${prefix}-${randomUUID().slice(0, 8)}`;
}
function skillMd(name: string) {
	return `---\nname: ${name}\ndescription: Skill folder acceptance example\n---\n\n# Instructions\nRead the linked resources.\n`;
}
async function headers() {
	return { "Content-Type": "application/json", Authorization: `Bearer ${await getAccessToken()}` };
}
async function createDraft(name: string, extra_files: Array<{ path: string; content: string; encoding?: "base64"; executable?: boolean }>) {
	const auth = await headers();
	const response = await fetch(`${API_BASE}/api/v1/skills/folder-drafts`, {
		method: "POST", headers: auth,
		body: JSON.stringify({ name, version: "1.0.0", description: "Skill folder acceptance example",
			owner: "admin", task_type: "general", skill_md_content: skillMd(name), extra_files }),
	});
	return { response, auth };
}
// No DELETE /skills/{id} exists. These uniquely named drafts may persist,
// so this suite must only run on a disposable test stack.
test.beforeEach(() => {
	test.skip(process.env.OBSERVAL_ISOLATED_STACK !== "true", "Creates drafts; use an isolated disposable test stack");
});

test("API preserves executable, empty and binary files in a versioned draft", async () => {
	const name = skillName("e2e-folder");
	const { response, auth } = await createDraft(name, [
		{ path: "scripts/run.sh", content: "echo ok\n", executable: true },
		{ path: "templates/empty.txt", content: "" },
		{ path: "assets/icon.bin", content: Buffer.from([0, 255]).toString("base64"), encoding: "base64" },
	]);
	expect(response.status).toBe(200);
	const draft = await response.json();
	expect(draft.listing_id).toBeTruthy();
	expect(draft.version_id).toBeTruthy();
	{
		const manifestRes = await fetch(`${API_BASE}/api/v1/skills/${draft.listing_id}/versions/${draft.version_id}/manifest`, { headers: auth });
		expect(manifestRes.status).toBe(200);
		const manifest = await manifestRes.json();
		expect(manifest.version_id).toBe(draft.version_id);
		expect(manifest.files.map((file: {path: string}) => file.path)).toEqual(
			expect.arrayContaining(["SKILL.md", "scripts/run.sh", "templates/empty.txt", "assets/icon.bin"])
		);
		expect(manifest.files.find((file: {path: string}) => file.path === "scripts/run.sh").mode).toBe("0755");
		expect(manifest.files.find((file: {path: string}) => file.path === "templates/empty.txt").size).toBe(0);
		const textRes = await fetch(`${API_BASE}/api/v1/skills/${draft.listing_id}/versions/${draft.version_id}/files/scripts/run.sh`, { headers: auth });
		expect(textRes.status).toBe(200);
		expect((await textRes.json()).content).toBe("echo ok\n");
		const binaryRes = await fetch(`${API_BASE}/api/v1/skills/${draft.listing_id}/versions/${draft.version_id}/files/assets/icon.bin`, { headers: auth });
		expect(binaryRes.headers.get("content-type")).toContain("application/octet-stream");
		expect(Buffer.from(await binaryRes.arrayBuffer())).toEqual(Buffer.from([0, 255]));
	}
});

test("browser creates a complete draft without submitting it for review", async ({ page }) => {
	const name = skillName("e2e-browser-folder");
	await loginToWebUI(page);
	await page.goto("/components?type=skills");
	await page.getByRole("button", { name: "Create", exact: true }).click();
	const dialog = page.getByRole("dialog");
	await dialog.getByRole("tab", { name: "Upload" }).click();
	await dialog.locator("#skill-file-upload").setInputFiles([
		{ name: "SKILL.md", mimeType: "text/markdown", buffer: Buffer.from(skillMd(name)) },
		{ name: "run.sh", mimeType: "text/plain", buffer: Buffer.from("echo browser\n") },
		{ name: "icon.bin", mimeType: "application/octet-stream", buffer: Buffer.from([0, 255]) },
	]);
	await expect(dialog.locator("#comp-name")).toHaveValue(name);
	await dialog.locator("#comp-version").fill("1.0.0");
	const responsePromise = page.waitForResponse((response) => response.url().endsWith("/api/v1/skills/folder-drafts") && response.request().method() === "POST");
	await dialog.getByRole("button", { name: "Save folder draft" }).click();
	const response = await responsePromise;
	expect(response.status(), await response.text()).toBe(200);
	const draft = await response.json();
	{
		expect(draft.files.map((file: {path: string}) => file.path)).toEqual(
			expect.arrayContaining(["SKILL.md", "run.sh", "icon.bin"])
		);
		expect(draft.files.find((file: {path: string}) => file.path === "icon.bin").size).toBe(2);
	}
});

test("browser resumes and saves the exact existing folder version", async ({ page }) => {
	const name = skillName("e2e-resume-folder");
	const { response, auth } = await createDraft(name, [
		{ path: "templates/note.txt", content: "before\n" },
		{ path: "assets/icon.bin", content: Buffer.from([0, 255]).toString("base64"), encoding: "base64" },
	]);
	expect(response.status).toBe(200);
	const draft = await response.json();
	await loginToWebUI(page);
	await page.goto("/components?type=skills");
	await page.getByRole("button", { name: /My submissions/ }).first().click();
	const row = page.locator("section .divide-y > div").filter({ hasText: name });
	await expect(row).toBeVisible();
	await row.getByRole("button", { name: "Edit" }).click();
	const choice = page.getByRole("dialog");
	await expect(choice.getByText("Choose the exact folder version")).toBeVisible();
	await choice.getByRole("button", { name: "Edit selected version" }).click();
	const editor = page.getByRole("dialog");
	await expect(editor.getByRole("tab", { name: "Upload" })).toHaveAttribute("data-state", "active");
	await expect(editor.getByText("assets/icon.bin")).toBeVisible();
	if (process.env.OBSERVAL_1730_SCREENSHOT_DIR) {
		await editor.getByText("assets/icon.bin").scrollIntoViewIfNeeded();
		await page.screenshot({ path: `${process.env.OBSERVAL_1730_SCREENSHOT_DIR}/folder-draft-editor.png`, fullPage: true, animations: "disabled" });
	}
	await editor.getByText("templates/note.txt").click();
	await editor.getByPlaceholder("File content...").fill("after\n");
	const save = page.waitForResponse((r) => r.url().includes(`/api/v1/skills/${draft.listing_id}/versions/${draft.version_id}/draft`) && r.request().method() === "PUT");
	await editor.getByRole("button", { name: "Save folder changes" }).click();
	expect((await save).status()).toBe(200);
	const versionsRes = await fetch(`${API_BASE}/api/v1/skills/${draft.listing_id}/versions`, { headers: auth });
	const versions = await versionsRes.json();
	expect(versions.total).toBe(1);
	expect(versions.items[0].id).toBe(draft.version_id);
	const textRes = await fetch(`${API_BASE}/api/v1/skills/${draft.listing_id}/versions/${draft.version_id}/files/templates/note.txt`, { headers: auth });
	expect((await textRes.json()).content).toBe("after\n");
	const binaryRes = await fetch(`${API_BASE}/api/v1/skills/${draft.listing_id}/versions/${draft.version_id}/files/assets/icon.bin`, { headers: auth });
	expect(Buffer.from(await binaryRes.arrayBuffer())).toEqual(Buffer.from([0, 255]));
	await row.getByRole("button", { name: "Submit", exact: true }).click();
	await expect(page.getByRole("dialog").getByText("Choose the exact folder version")).toBeVisible();
	const denied = page.waitForResponse((r) => r.url().endsWith(`/api/v1/skills/${draft.listing_id}/versions/${draft.version_id}/submit`));
	await page.getByRole("dialog").getByRole("button", { name: "Submit selected version" }).click();
	expect((await denied).status()).toBe(409);
	const current = await fetch(`${API_BASE}/api/v1/skills/${draft.listing_id}/versions`, { headers: auth });
	expect((await current.json()).items[0].status).toBe("draft");
});

test("gated reviewer and installer compare exact candidate with reviewed base", async ({ page }) => {
	test.skip(process.env.OBSERVAL_SKILL_FOLDER_DELIVERY_ENABLED !== "true", "Run only on isolated temporary gate-on stack");
	const listingId = process.env.OBSERVAL_GATED_LISTING_ID!;
	const versionId = process.env.OBSERVAL_GATED_VERSION_ID!;
	await loginToWebUI(page);
	await page.goto("/review?tab=components");
	await page.getByRole("tab", { name: /components/i }).click();
	await page.locator(`[data-review-item="skill:${versionId}"]`).click();
	await page.getByRole("button", { name: "View full diff" }).click();
	const sheet = page.getByRole("dialog");
	await expect(sheet.getByText("Compared to base: 2 added, 1 modified, 1 removed.")).toBeVisible();
	await sheet.getByRole("button", { name: "templates", exact: true }).click();
	await sheet.getByRole("button", { name: /note.txt/ }).click();
	await expect(sheet.getByText("after", { exact: true })).toBeVisible();
	await expect(sheet.getByText("before", { exact: true })).toBeVisible();
	await expect(sheet.getByRole("button", { name: "templates/removed.txt Removed" })).toBeVisible();
	if (process.env.OBSERVAL_1730_SCREENSHOT_DIR) {
		await page.screenshot({ path: `${process.env.OBSERVAL_1730_SCREENSHOT_DIR}/folder-review-diff.png`, fullPage: true });
	}
	const review = await fetch(`${API_BASE}/api/v1/review/skills/${listingId}/versions/${versionId}`, { headers: await headers() });
	expect(review.status).toBe(200);
	await page.setViewportSize({ width: 390, height: 844 });
	await page.goto(`/components/${listingId}?type=skills`);
	const install = page.locator("aside");
	await expect(install.getByText("Install", { exact: true })).toBeVisible();
	const harnessResponse = await fetch(`${API_BASE}/api/v1/config/harnesses`);
	const harnessList = await harnessResponse.json();
	const skillHarnesses = harnessList.harnesses.filter((harness: {capabilities: string[]}) => harness.capabilities.includes("skills"));
	await install.getByRole("combobox").click();
	await expect(page.getByRole("option")).toHaveCount(skillHarnesses.length);
	await page.getByRole("option").first().click();
	const copy = install.getByRole("button", { name: "Copy command" });
	await copy.scrollIntoViewIfNeeded();
	await expect(copy).toBeVisible();
	await expect(install.locator("code")).toContainText("observal registry skill install");
	if (process.env.OBSERVAL_1730_SCREENSHOT_DIR) {
		await page.screenshot({ path: `${process.env.OBSERVAL_1730_SCREENSHOT_DIR}/skill-install-mobile.png`, fullPage: true, animations: "disabled" });
	}
});

test("resource-bearing draft cannot be submitted while delivery gate remains off", async () => {
	test.skip(process.env.OBSERVAL_SKILL_FOLDER_DELIVERY_ENABLED === "true", "Use the gated reviewer flow on the isolated enabled stack");
	const { response, auth } = await createDraft(skillName("e2e-gated-review"), [{ path: "scripts/run.sh", content: "echo gated" }]);
	expect(response.status).toBe(200);
	const draft = await response.json();
	{
		const submit = await fetch(`${API_BASE}/api/v1/skills/${draft.listing_id}/versions/${draft.version_id}/submit`, {
			method: "POST", headers: auth, body: JSON.stringify({ observed_revision: draft.revision }),
		});
		expect(submit.status).toBe(409);
	}
});

test("refuses invalid base64 rather than silently accepting partial bytes", async () => {
	const name = skillName("e2e-invalid-base64");
	const { response } = await createDraft(name, [{ path: "scripts/run.sh", content: "not base64!", encoding: "base64" }]);
	expect([400, 422]).toContain(response.status);
});

test("refuses traversal and excessive file counts", async () => {
	const traversal = await createDraft(skillName("e2e-traversal"), [{ path: "../outside.txt", content: "bad" }]);
	expect([400, 422]).toContain(traversal.response.status);
	const tooMany = await createDraft(skillName("e2e-file-count"),
		Array.from({ length: 129 }, (_, n) => ({ path: `templates/${n}.txt`, content: "" })));
	expect([400, 422]).toContain(tooMany.response.status);
});
