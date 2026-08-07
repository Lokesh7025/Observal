// SPDX-FileCopyrightText: 2026 Lokesh Selvam <lokeshselvam7025@gmail.com>
// SPDX-License-Identifier: Apache-2.0

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { inbox } from "@/lib/api";
import type { InboxFilters } from "@/lib/types";

const INBOX_KEY = ["inbox"] as const;

export function useInbox(filters: InboxFilters = {}) {
	return useQuery({
		queryKey: [...INBOX_KEY, "list", filters],
		queryFn: () => inbox.list(filters),
		retry: false,
	});
}

export function useInboxCounts(enabled = true) {
	return useQuery({
		queryKey: [...INBOX_KEY, "count"],
		queryFn: () => inbox.counts(),
		// The badge should feel live without hammering the API. A minute is
		// short enough that a review assignment does not sit unseen for long.
		// Polling was chosen over SSE deliberately: server-sent events would
		// need a connection story for the multi-replica ECS/Fargate topology,
		// and a badge does not justify one.
		refetchInterval: 60_000,
		refetchOnWindowFocus: true,
		enabled,
		retry: false,
	});
}

export function useInboxItem(id: string | null) {
	return useQuery({
		queryKey: [...INBOX_KEY, "detail", id],
		queryFn: () => inbox.detail(id as string),
		enabled: Boolean(id),
		retry: false,
	});
}

/**
 * Every mutation invalidates the whole inbox namespace rather than patching
 * one row: marking an item read changes the badge counts and can change which
 * filtered lists it belongs to, and a partial update would leave those stale.
 */
function useInboxMutation<TArgs>(
	fn: (args: TArgs) => Promise<unknown>,
	errorMessage: string,
) {
	const qc = useQueryClient();
	return useMutation({
		mutationFn: fn,
		onSuccess: () => {
			qc.invalidateQueries({ queryKey: INBOX_KEY });
		},
		// Without this the row simply does not change and the click reads as a
		// no-op, which is indistinguishable from a broken button.
		onError: () => {
			toast.error(errorMessage);
		},
	});
}

export function useMarkRead() {
	return useInboxMutation(
		(id: string) => inbox.read(id),
		"Could not mark that as read. Please try again.",
	);
}

export function useMarkUnread() {
	return useInboxMutation(
		(id: string) => inbox.unread(id),
		"Could not mark that as unread. Please try again.",
	);
}

export function useMarkDone() {
	return useInboxMutation(
		(id: string) => inbox.done(id),
		"Could not resolve that item. Please try again.",
	);
}

export function useDismissItem() {
	return useInboxMutation(
		(id: string) => inbox.dismiss(id),
		"Could not dismiss that item. Please try again.",
	);
}

export function useReopenItem() {
	return useInboxMutation(
		(id: string) => inbox.reopen(id),
		"Could not reopen that item. Please try again.",
	);
}

export function useReadAll() {
	return useInboxMutation(
		(filters: InboxFilters) => inbox.readAll(filters),
		"Could not mark those as read. Please try again.",
	);
}
