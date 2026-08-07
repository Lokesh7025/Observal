// SPDX-FileCopyrightText: 2026 Lokesh Selvam <lokeshselvam7025@gmail.com>
// SPDX-License-Identifier: Apache-2.0

export type InboxKind =
	| "review_requested"
	| "review_approved"
	| "review_rejected"
	| "review_comment"
	| "change_requested"
	| "team_join_requested"
	| "team_join_decided"
	| "team_created_pending"
	| "ownership_transfer"
	| "update_available"
	| "insight_ready"
	| "system_notice";

export type InboxState = "open" | "done" | "dismissed";

export interface InboxItem {
	id: string;
	kind: InboxKind;
	state: InboxState;
	/** Independent of `state` — an item can be read and still be open work. */
	read: boolean;
	read_at: string | null;
	action_required: boolean;
	title: string;
	body: string | null;
	subject_type: string;
	subject_id: string | null;
	subject_namespace: string | null;
	subject_slug: string | null;
	action_url: string | null;
	action_command: string | null;
	actor_id: string | null;
	team_id: string | null;
	payload: Record<string, unknown>;
	created_at: string;
	resolved_at: string | null;
}

export interface InboxItemEvent {
	id: string;
	event: string;
	actor_id: string | null;
	detail: string | null;
	created_at: string;
}

export interface InboxItemDetail extends InboxItem {
	history: InboxItemEvent[];
}

export interface InboxListResponse {
	items: InboxItem[];
	total: number;
	page: number;
	page_size: number;
}

export interface InboxCounts {
	unread: number;
	action_required: number;
	open: number;
}

export interface InboxFilters {
	state?: InboxState;
	kind?: InboxKind;
	action_required?: boolean;
	unread?: boolean;
	subject_type?: string;
	page?: number;
	page_size?: number;
}

export const INBOX_KIND_LABELS: Record<InboxKind, string> = {
	review_requested: "Review requested",
	review_approved: "Approved",
	review_rejected: "Changes needed",
	review_comment: "Comment",
	change_requested: "Changes requested",
	team_join_requested: "Join request",
	team_join_decided: "Join decision",
	team_created_pending: "Teamspace pending",
	ownership_transfer: "Ownership transfer",
	update_available: "Update available",
	insight_ready: "Insight ready",
	system_notice: "System notice",
};
