// SPDX-FileCopyrightText: 2026 Lokesh Selvam <lokeshselvam7025@gmail.com>
// SPDX-License-Identifier: Apache-2.0

import { createFileRoute } from "@tanstack/react-router";
import { lazy } from "react";

const InboxPage = lazy(() => import("@/pages/user/inbox"));

export const Route = createFileRoute("/_authed/_user/inbox")({
	component: InboxPage,
});
