// SPDX-FileCopyrightText: 2026 Lokesh Selvam <lokeshselvam7025@gmail.com>
// SPDX-License-Identifier: Apache-2.0

import { useMemo, useState } from "react";
import { Link } from "@tanstack/react-router";
import {
	Check,
	CheckCheck,
	Copy,
	Inbox as InboxIcon,
	Loader2,
	RotateCcw,
	X,
} from "lucide-react";
import { toast } from "sonner";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { PageHeader } from "@/components/layouts/page-header";
import { DashboardContent, DashboardShell } from "@/components/layouts/dashboard-shell";
import {
	useDismissItem,
	useInbox,
	useInboxCounts,
	useInboxItem,
	useMarkDone,
	useMarkRead,
	useMarkUnread,
	useReadAll,
	useReopenItem,
} from "@/hooks/use-inbox-api";
import { INBOX_KIND_LABELS, type InboxFilters, type InboxItem } from "@/lib/types";
import { cn } from "@/lib/utils";

type RailKey = "all" | "action" | "unread" | "done";

const RAIL: { key: RailKey; label: string; filters: InboxFilters }[] = [
	{ key: "all", label: "All open", filters: { state: "open" } },
	{ key: "action", label: "Needs action", filters: { state: "open", action_required: true } },
	{ key: "unread", label: "Unread", filters: { unread: true } },
	{ key: "done", label: "Resolved", filters: { state: "done" } },
];

function relativeTime(iso: string): string {
	const then = new Date(iso).getTime();
	const mins = Math.round((Date.now() - then) / 60000);
	if (mins < 1) return "just now";
	if (mins < 60) return `${mins}m ago`;
	const hours = Math.round(mins / 60);
	if (hours < 24) return `${hours}h ago`;
	return `${Math.round(hours / 24)}d ago`;
}

function ItemRow({
	item,
	selected,
	onSelect,
}: {
	item: InboxItem;
	selected: boolean;
	onSelect: () => void;
}) {
	return (
		<button
			type="button"
			onClick={onSelect}
			className={cn(
				"w-full border-b px-3 py-2.5 text-left transition-colors hover:bg-muted/50",
				selected && "bg-muted",
			)}
		>
			<div className="flex items-start gap-2">
				{/* Unread is a dot, not bold-everything: the list stays scannable
				    when most items are unread. */}
				<span
					className={cn(
						"mt-1.5 h-2 w-2 shrink-0 rounded-full",
						item.read ? "bg-transparent" : "bg-primary",
					)}
					aria-label={item.read ? undefined : "Unread"}
				/>
				<div className="min-w-0 flex-1">
					<div className={cn("truncate text-sm", !item.read && "font-medium")}>
						{item.title}
					</div>
					<div className="mt-1 flex flex-wrap items-center gap-1.5">
						<Badge variant="outline" className="text-[10px]">
							{INBOX_KIND_LABELS[item.kind] ?? item.kind}
						</Badge>
						{item.action_required && item.state === "open" && (
							<Badge variant="default" className="text-[10px]">
								Needs action
							</Badge>
						)}
						{item.state !== "open" && (
							<Badge variant="secondary" className="text-[10px]">
								{item.state}
							</Badge>
						)}
						<span className="text-[10px] text-muted-foreground">
							{relativeTime(item.created_at)}
						</span>
					</div>
				</div>
			</div>
		</button>
	);
}

function DetailPane({ itemId }: { itemId: string | null }) {
	const { data: item, isLoading } = useInboxItem(itemId);
	const markRead = useMarkRead();
	const markUnread = useMarkUnread();
	const markDone = useMarkDone();
	const dismiss = useDismissItem();
	const reopen = useReopenItem();

	if (!itemId) {
		return (
			<div className="flex h-full items-center justify-center p-6 text-sm text-muted-foreground">
				Select an item to see its detail and history.
			</div>
		);
	}
	if (isLoading || !item) {
		return (
			<div className="flex h-full items-center justify-center p-6">
				<Loader2 className="h-4 w-4 animate-spin text-muted-foreground" />
			</div>
		);
	}

	const copyCommand = async () => {
		if (!item.action_command) return;
		try {
			await navigator.clipboard.writeText(item.action_command);
			toast.success("Command copied");
		} catch {
			toast.error("Could not copy the command");
		}
	};

	return (
		<div className="flex h-full flex-col overflow-y-auto p-4">
			<div className="mb-1 flex items-start justify-between gap-3">
				<h2 className="text-base font-semibold">{item.title}</h2>
				<Badge variant="outline" className="shrink-0 text-[10px]">
					{INBOX_KIND_LABELS[item.kind] ?? item.kind}
				</Badge>
			</div>
			<div className="mb-3 text-xs text-muted-foreground">
				{new Date(item.created_at).toLocaleString()}
				{item.state !== "open" && ` · ${item.state}`}
			</div>

			{item.body && (
				<p className="mb-4 whitespace-pre-wrap text-sm text-muted-foreground">{item.body}</p>
			)}

			<div className="mb-4 flex flex-wrap gap-2">
				{item.action_url && (
					<Button asChild size="sm">
						<Link to={item.action_url}>Open</Link>
					</Button>
				)}
				{item.state === "open" ? (
					<>
						<Button
							size="sm"
							variant="outline"
							onClick={() => markDone.mutate(item.id)}
							disabled={markDone.isPending}
						>
							<Check className="mr-1 h-3.5 w-3.5" />
							Done
						</Button>
						<Button
							size="sm"
							variant="ghost"
							onClick={() => dismiss.mutate(item.id)}
							disabled={dismiss.isPending}
						>
							<X className="mr-1 h-3.5 w-3.5" />
							Dismiss
						</Button>
					</>
				) : (
					<Button
						size="sm"
						variant="outline"
						onClick={() => reopen.mutate(item.id)}
						disabled={reopen.isPending}
					>
						<RotateCcw className="mr-1 h-3.5 w-3.5" />
						Reopen
					</Button>
				)}
				<Button
					size="sm"
					variant="ghost"
					onClick={() =>
						item.read ? markUnread.mutate(item.id) : markRead.mutate(item.id)
					}
				>
					{item.read ? "Mark unread" : "Mark read"}
				</Button>
			</div>

			{item.action_command && (
				<div className="mb-4">
					<div className="mb-1 text-xs font-medium text-muted-foreground">
						Run this yourself
					</div>
					{/* Shown, never executed: the web app does not run commands. */}
					<div className="flex items-center gap-2 rounded-md border bg-muted/40 p-2">
						<code className="min-w-0 flex-1 overflow-x-auto whitespace-nowrap text-xs">
							{item.action_command}
						</code>
						<Button size="icon" variant="ghost" className="h-6 w-6" onClick={copyCommand}>
							<Copy className="h-3 w-3" />
						</Button>
					</div>
				</div>
			)}

			<div className="mt-auto pt-4">
				<div className="mb-2 text-xs font-medium text-muted-foreground">History</div>
				<ol className="space-y-1">
					{item.history.map((event) => (
						<li key={event.id} className="flex items-baseline gap-2 text-xs">
							<span className="font-mono text-muted-foreground">
								{new Date(event.created_at).toLocaleString()}
							</span>
							<span>{event.event}</span>
							{event.detail && (
								<span className="text-muted-foreground">— {event.detail}</span>
							)}
						</li>
					))}
				</ol>
			</div>
		</div>
	);
}

export default function InboxPage() {
	const [rail, setRail] = useState<RailKey>("all");
	const [selected, setSelected] = useState<string | null>(null);

	const filters = useMemo(
		() => RAIL.find((r) => r.key === rail)?.filters ?? {},
		[rail],
	);
	const { data, isLoading } = useInbox(filters);
	const { data: counts } = useInboxCounts();
	const readAll = useReadAll();

	const items = data?.items ?? [];

	return (
		<DashboardShell>
			<PageHeader title="Inbox" breadcrumbs={[{ label: "Inbox" }]} />
			<DashboardContent className="p-0">
				<div className="flex h-full min-h-0 flex-col md:flex-row">
					{/* Filter rail */}
					<aside className="shrink-0 border-b p-2 md:w-52 md:border-b-0 md:border-r">
						<nav className="flex gap-1 overflow-x-auto md:flex-col md:overflow-x-visible">
							{RAIL.map((entry) => {
								const badge =
									entry.key === "action"
										? counts?.action_required
										: entry.key === "unread"
											? counts?.unread
											: undefined;
								return (
									<button
										key={entry.key}
										type="button"
										onClick={() => setRail(entry.key)}
										className={cn(
											"flex shrink-0 items-center justify-between gap-2 rounded-md px-3 py-1.5 text-sm transition-colors md:shrink",
											rail === entry.key
												? "bg-muted font-medium"
												: "hover:bg-muted/50",
										)}
									>
										<span>{entry.label}</span>
										{badge ? (
											<Badge variant="secondary" className="text-[10px]">
												{badge}
											</Badge>
										) : null}
									</button>
								);
							})}
						</nav>
						<div className="mt-3 hidden md:block">
							<Button
								size="sm"
								variant="ghost"
								className="w-full justify-start text-xs"
								disabled={readAll.isPending || items.length === 0}
								onClick={() => readAll.mutate(filters)}
							>
								<CheckCheck className="mr-1.5 h-3.5 w-3.5" />
								Mark these read
							</Button>
						</div>
					</aside>

					{/* List */}
					<div className="min-h-0 flex-1 overflow-y-auto border-b md:max-w-md md:border-b-0 md:border-r">
						{isLoading ? (
							<div className="flex items-center justify-center p-8">
								<Loader2 className="h-4 w-4 animate-spin text-muted-foreground" />
							</div>
						) : items.length === 0 ? (
							<Card className="m-3 border-dashed shadow-none">
								<CardContent className="flex flex-col items-center gap-2 p-6 text-center">
									<InboxIcon className="h-6 w-6 text-muted-foreground" />
									<p className="text-sm font-medium">Nothing here</p>
									<p className="text-xs text-muted-foreground">
										Review assignments, decisions on your submissions, and update
										notices show up here.
									</p>
								</CardContent>
							</Card>
						) : (
							items.map((item) => (
								<ItemRow
									key={item.id}
									item={item}
									selected={selected === item.id}
									onSelect={() => setSelected(item.id)}
								/>
							))
						)}
					</div>

					{/* Detail */}
					<div className="min-h-0 flex-1">
						<DetailPane itemId={selected} />
					</div>
				</div>
			</DashboardContent>
		</DashboardShell>
	);
}
