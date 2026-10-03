<!-- SPDX-FileCopyrightText: 2026 Observal Contributors -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

# PR #1781 Screenshots Required

This document lists the screenshots needed for PR #1781 (skill folder functionality).
Screenshots should be captured and attached to the PR body before merge.

## 1. Submit Component Dialog - Folder Tab

**Location:** Registry → Skills → Submit
**Component:** `submit-component-dialog.tsx`

Screenshots needed:
- [ ] Folder tab selected (showing SKILL.md editor and extra files section)
- [ ] After adding extra files (showing file list with executable indicators)
- [ ] File content editor (showing selected file with executable checkbox)

## 2. Component Edit Form - Folder Tab

**Location:** Registry → Skills → [Skill] → Edit
**Component:** `component-edit-form.tsx`

Screenshots needed:
- [ ] Folder tab selected with existing files loaded
- [ ] File editing interface
- [ ] Add/remove file interactions

## 3. Review Detail Sheet - File Tree

**Location:** Review Queue → [Skill with extra files]
**Component:** `review-detail-sheet.tsx`

Screenshots needed:
- [ ] Review sheet showing file tree section for a skill with extra files
- [ ] Expanded file tree with nested directories
- [ ] File content preview when clicking a file

## 4. Skill File Tree Component

**Component:** `skill-file-tree.tsx`

This component is used in both review and editing contexts. Screenshots should
show both authoring mode (with actions dropdown) and review mode (read-only).

## Light/Dark Mode

All screenshots should be captured in both light and dark modes.

## Accessibility

Verify and document:
- [ ] Keyboard navigation works for file tree
- [ ] Focus indicators are visible
- [ ] Screen reader announces file tree structure

---

**Note for reviewers:** Until screenshots are attached, consider this PR
as requiring frontend review. The UI changes are:

1. **Submit dialog:** Three-column tabs (Git | Single File | Folder)
2. **Edit form:** Three-column tabs (Git | Pasted files | Folder)
3. **Review sheet:** Collapsible file tree with content preview
