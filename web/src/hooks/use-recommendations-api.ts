// SPDX-FileCopyrightText: 2026 Lokesh Selvam <lokeshselvam7025@gmail.com>
// SPDX-License-Identifier: Apache-2.0

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { recommendations } from "@/lib/api";

export function useMyRecommendations(limit = 8, type?: string) {
	return useQuery({
		queryKey: ["recommendations", "me", limit, type ?? "all"],
		queryFn: () => recommendations.me(limit, type),
		// The profile is cached server-side; refetching on every focus would
		// just add load without changing the answer.
		staleTime: 5 * 60 * 1000,
		retry: false,
	});
}

export function useDismissRecommendation() {
	const qc = useQueryClient();
	return useMutation({
		mutationFn: ({ type, id }: { type: string; id: string }) =>
			recommendations.feedback(type, id, "dismissed"),
		onSuccess: () => {
			qc.invalidateQueries({ queryKey: ["recommendations"] });
		},
	});
}
