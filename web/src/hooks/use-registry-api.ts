// SPDX-FileCopyrightText: 2026 Aryan Iyappan <aryaniyappan2006@gmail.com>
// SPDX-FileCopyrightText: 2026 Harishankar <harishankar0301@gmail.com>
// SPDX-FileCopyrightText: 2026 Hari Srinivasan <harisrini21@gmail.com>
// SPDX-FileCopyrightText: 2026 Kaushik Kumar <kaushikrjpm10@gmail.com>
// SPDX-FileCopyrightText: 2026 Lokesh Selvam <lokeshselvam7025@gmail.com>
// SPDX-FileCopyrightText: 2026 Shaan Narendran <shaannaren06@gmail.com>
// SPDX-FileCopyrightText: 2026 Shreem Seth <shreemseth26@gmail.com>
// SPDX-FileCopyrightText: 2026 SrihariLegend <sriharilegend23@gmail.com>
// SPDX-FileCopyrightText: 2026 Vishnu Muthiah <vishnu.muthiah04@gmail.com>
// SPDX-License-Identifier: Apache-2.0


import {
  useQuery,
  useMutation,
  useQueryClient,
} from "@tanstack/react-query";
import { toast } from "sonner";
import type { SkillFolderDraftRequest, SkillResource, SkillSuccessorRequest } from "@/lib/types";
import {
  registry,
  type RegistryType,
} from "@/lib/api";

// ── Component Draft/Submit (generic) ──────────────────────────────

function isSkillFolderDraft(type: RegistryType, body: unknown): body is SkillFolderDraftRequest {
  return type === "skills" && body !== null && typeof body === "object" &&
    "extra_files" in body && Array.isArray(body.extra_files);
}

export function useMyComponents(type: RegistryType, enabled = true) {
  return useQuery({
    queryKey: ["registry", type, "my"],
    queryFn: () => registry.my(type),
    enabled,
  });
}

export function useUpdateRegistryVisibility() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ type, id, visibility }: { type: RegistryType; id: string; visibility: "public" | "team" }) =>
      registry.updateVisibility(type, id, visibility),
    onSuccess: (_data, variables) => {
      qc.invalidateQueries({ queryKey: ["registry", variables.type] });
      qc.invalidateQueries({ queryKey: ["registry", variables.type, variables.id] });
      toast.success("Visibility updated");
    },
    onError: (err: Error) => toast.error(err.message || "Failed to update visibility"),
  });
}

export function useComponentSubmit(type: RegistryType) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async (body: unknown) => isSkillFolderDraft(type, body) ? await registry.folderDraft(body) : await registry.submit(type, body),
    onSuccess: (_data, body) => {
      qc.invalidateQueries({ queryKey: ["registry", type] });
      qc.invalidateQueries({ queryKey: ["review"] });
      toast.success(isSkillFolderDraft(type, body) ? "Folder draft saved; submit after delivery is enabled" : "Submitted for review");
    },
    onError: (err: Error) => {
      toast.error(err.message || "Failed to submit");
    },
  });
}

export function useComponentSaveDraft(type: RegistryType) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async (body: unknown) => isSkillFolderDraft(type, body) ? await registry.folderDraft(body) : await registry.draft(body, type),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["registry", type] });
      toast.success("Draft saved");
    },
    onError: (err: Error) => {
      toast.error(err.message || "Failed to save draft");
    },
  });
}

export function useComponentUpdateDraft(type: RegistryType) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (vars: { id: string; body: unknown }) =>
      registry.updateDraft(vars.id, vars.body, type),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["registry", type] });
      toast.success("Draft updated");
    },
    onError: (err: Error) => {
      toast.error(err.message || "Failed to update draft");
    },
  });
}

export function useComponentSubmitDraft(type: RegistryType) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => registry.submitDraft(id, type),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["registry", type] });
      qc.invalidateQueries({ queryKey: ["review"] });
      toast.success("Submitted for review");
    },
    onError: (err: Error) => {
      toast.error(err.message || "Failed to submit");
    },
  });
}

export function useStartEdit(type: RegistryType) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => registry.startEdit(id, type),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["review"] });
    },
    onError: (err: Error) => {
      toast.error(err.message || "Failed to start editing");
    },
  });
}

export function useCancelEdit(type: RegistryType) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => registry.cancelEdit(id, type),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["review"] });
    },
    onError: (err: Error) => {
      toast.error(err.message || "Failed to cancel editing");
    },
  });
}

export function useComponentArchive(type: RegistryType) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => registry.archiveComponent(type, id),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["registry", type] });
      toast.success("Component archived");
    },
    onError: (err: Error) => {
      toast.error(err.message || "Failed to archive component");
    },
  });
}

export function useComponentUnarchive(type: RegistryType) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => registry.unarchiveComponent(type, id),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["registry", type] });
      toast.success("Component restored");
    },
    onError: (err: Error) => {
      toast.error(err.message || "Failed to restore component");
    },
  });
}

// ── Component Versions ─────────────────────────────────────────────

export function useComponentVersions(type: RegistryType | undefined, listingId: string | undefined) {
  return useQuery({
    queryKey: ["component-versions", type, listingId],
    enabled: !!type && !!listingId,
    queryFn: () => registry.listComponentVersions(type!, listingId!),
  });
}

export function useComponentVersionsPage(type: RegistryType | undefined, listingId: string | undefined, page: number) {
  return useQuery({
    queryKey: ["component-versions", type, listingId, "page", page],
    enabled: !!type && !!listingId && page > 1,
    queryFn: () => registry.listComponentVersions(type!, listingId!, page, 50),
  });
}

export function useComponentVersionDetail(type: RegistryType | undefined, listingId: string | undefined, version: string | null) {
  return useQuery({
    queryKey: ["component-version-detail", type, listingId, version],
    enabled: !!type && !!listingId && !!version,
    queryFn: () => registry.getComponentVersion(type!, listingId!, version!),
  });
}

export function usePublishComponentVersion() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ type, listingId, body }: { type: RegistryType; listingId: string; body: unknown }) =>
      registry.publishComponentVersion(type, listingId, body),
    onSuccess: (_data, variables) => {
      qc.invalidateQueries({ queryKey: ["component-versions", variables.type, variables.listingId] });
      qc.invalidateQueries({ queryKey: ["registry", variables.type, variables.listingId] });
      toast.success("Version published successfully");
    },
    onError: (err: Error) => {
      toast.error(err.message || "Failed to publish version");
    },
  });
}

export function useComponentVersionSuggestions(type: RegistryType | undefined, listingId: string | undefined) {
  return useQuery({
    queryKey: ["component-version-suggestions", type, listingId],
    enabled: !!type && !!listingId,
    queryFn: () => registry.componentVersionSuggestions(type!, listingId!),
  });
}

// ── Skill Folder Version APIs ──────────────────────────────────────

export function useSkillApprovedBase(listingId: string | undefined, enabled: boolean) {
  return useQuery({
    queryKey: ["skill-approved-base", listingId],
    enabled: enabled && !!listingId,
    staleTime: 0,
    queryFn: () => registry.getSkillApprovedBase(listingId!),
  });
}

export function useImportSkillFolder() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ listingId, body }: {
      listingId: string; body: SkillSuccessorRequest & { skill_md_content: string; extra_files: SkillResource[] };
    }) => registry.importSkillFolder(listingId, body),
    onSuccess: (_data, vars) => {
      qc.invalidateQueries({ queryKey: ["component-versions", "skills", vars.listingId] });
      qc.invalidateQueries({ queryKey: ["registry", "skills"] });
    },
    onError: (err: Error) => toast.error(err.message || "Could not import the complete folder"),
  });
}

export function useCreateSkillSuccessor() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ listingId, body }: { listingId: string; body: SkillSuccessorRequest }) =>
      registry.forkSkillVersion(listingId, body),
    onSuccess: (_data, vars) => {
      qc.invalidateQueries({ queryKey: ["component-versions", "skills", vars.listingId] });
      qc.invalidateQueries({ queryKey: ["registry", "skills"] });
    },
    onError: (err: Error) => toast.error(err.message || "Could not create a folder version"),
  });
}

async function verifiedSkillFile(bytes: ArrayBuffer, expectedHash: string): Promise<void> {
  const digest = await crypto.subtle.digest("SHA-256", bytes);
  const hash = Array.from(new Uint8Array(digest), (byte) => byte.toString(16).padStart(2, "0")).join("");
  if (hash !== expectedHash) throw new Error("Saved folder file changed while loading; retry from the latest manifest");
}

function encodeBinary(bytes: Uint8Array): string {
  const chunks: string[] = [];
  for (let offset = 0; offset < bytes.length; offset += 8192) {
    chunks.push(String.fromCharCode(...bytes.subarray(offset, offset + 8192)));
  }
  return btoa(chunks.join(""));
}

/** Load one exact saved version, verifying every fetched byte before offering an editor. */
export function useLoadSkillFolderDraft() {
  return useMutation({
    mutationFn: async ({ listingId, versionId }: { listingId: string; versionId: string }) => {
      const manifest = await registry.getSkillVersionManifest(listingId, versionId);
      const files = await Promise.all(manifest.files.map(async (file) => {
        const result = await registry.getSkillFileContent(listingId, versionId, file.path);
        if (result.encoding === "utf-8" && (result.version_id !== versionId || result.revision !== manifest.revision)) {
          throw new Error("Saved folder changed while loading; retry from the latest manifest");
        }
        const bytes = result.encoding === "binary"
          ? await result.content.arrayBuffer() : new TextEncoder().encode(result.content).buffer;
        if (bytes.byteLength !== file.size) throw new Error("Saved folder file size changed; retry");
        await verifiedSkillFile(bytes, file.sha256);
        return {
          path: file.path,
          content: result.encoding === "binary" ? encodeBinary(new Uint8Array(bytes)) : result.content,
          ...(result.encoding === "binary" ? { encoding: "base64" as const } : {}),
          executable: file.mode === "0755",
        } satisfies SkillResource;
      }));
      const latest = await registry.getSkillVersionManifest(listingId, versionId);
      if (manifest.revision !== latest.revision) throw new Error("Saved folder changed while loading; retry");
      const skillMd = files.find((file) => file.path === "SKILL.md");
      if (!skillMd || skillMd.encoding === "base64") throw new Error("Saved folder has no text SKILL.md");
      return {
        revision: manifest.revision,
        skill_md_content: skillMd.content,
        extra_files: files.filter((file) => file.path !== "SKILL.md"),
      };
    },
    onError: (err: Error) => toast.error(err.message || "Failed to open saved folder"),
  });
}

export function useUpdateSkillFolderDraft() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ listingId, versionId, observedRevision, body }: {
      listingId: string; versionId: string; observedRevision: string; body: Record<string, unknown>;
    }) => registry.updateSkillVersionDraft(listingId, versionId, {
      observed_revision: observedRevision,
      description: body.description,
      task_type: body.task_type,
      supported_harnesses: body.supported_harnesses,
      skill_md_content: body.skill_md_content,
      extra_files: body.extra_files,
    }),
    onSuccess: (_data, vars) => {
      qc.invalidateQueries({ queryKey: ["registry", "skills"] });
      qc.invalidateQueries({ queryKey: ["component-versions", "skills", vars.listingId] });
      qc.invalidateQueries({ queryKey: ["skill-version-manifest", vars.listingId, vars.versionId] });
      toast.success("Saved the same folder version");
    },
    onError: (err: Error) => toast.error(err.message || "Failed to save folder version"),
  });
}

export function useSubmitSkillFolderDraft() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ listingId, versionId, observedRevision }: {
      listingId: string; versionId: string; observedRevision: string;
    }) => registry.submitSkillVersionDraft(listingId, versionId, observedRevision),
    onSuccess: (_data, vars) => {
      qc.invalidateQueries({ queryKey: ["registry", "skills"] });
      qc.invalidateQueries({ queryKey: ["component-versions", "skills", vars.listingId] });
      qc.invalidateQueries({ queryKey: ["review"] });
      toast.success("Submitted exact folder version for review");
    },
    onError: (err: Error) => toast.error(err.message || "Failed to submit folder version"),
  });
}

export function useSkillVersionManifest(listingId: string | undefined, versionId: string | undefined) {
  return useQuery({
    queryKey: ["skill-version-manifest", listingId, versionId],
    enabled: !!listingId && !!versionId,
    queryFn: () => registry.getSkillVersionManifest(listingId!, versionId!),
  });
}

export function useSkillFileContent(listingId: string | undefined, versionId: string | undefined, filePath: string | null) {
  return useQuery({
    queryKey: ["skill-file-content", listingId, versionId, filePath],
    enabled: !!listingId && !!versionId && !!filePath,
    queryFn: () => registry.getSkillFileContent(listingId!, versionId!, filePath!),
  });
}
