# SPDX-FileCopyrightText: 2026 Lokesh Selvam <lokeshselvam7025@gmail.com>
# SPDX-License-Identifier: Apache-2.0

"""observal inbox: your work and event feed.

Self-only by design, matching the server: there is no route that reads another
user's inbox, so there is no flag for one here either.

Output follows this repo's existing ``--output table|json`` convention rather
than inventing a second one. When the universal CLI contract lands, these
commands adopt it along with every other command.
"""

from __future__ import annotations

from urllib.parse import urlencode

import typer
from rich import print as rprint
from rich.table import Table

from observal_cli import client
from observal_cli.render import console, esc, output_json, spinner

inbox_app = typer.Typer(
    name="inbox",
    help="Your work and event feed: reviews, decisions, and update notices",
    no_args_is_help=False,
    invoke_without_command=True,
)

_STATES = ("open", "done", "dismissed")

_KINDS = (
    "review_requested",
    "review_approved",
    "review_rejected",
    "review_comment",
    "change_requested",
    "team_join_requested",
    "team_join_decided",
    "team_created_pending",
    "ownership_transfer",
    "update_available",
    "insight_ready",
    "system_notice",
)


def _validate(value: str | None, allowed: tuple[str, ...], label: str) -> str | None:
    if value is None:
        return None
    normalized = value.strip().lower()
    if normalized not in allowed:
        rprint(f"[red]Unknown {label} '{esc(value)}'.[/red]")
        rprint(f"[dim]Choose one of: {', '.join(allowed)}[/dim]")
        raise typer.Exit(1)
    return normalized


def _filters(
    state: str | None,
    kind: str | None,
    action_required: bool,
    unread: bool = False,
) -> dict[str, object]:
    """Only the filters the user actually passed — no paging defaults.

    read-all builds its confirmation prompt from this dict, so implicit
    ``page``/``page_size`` entries would make an unfiltered run read as
    page-bounded when the server marks everything matching the filter.
    """
    params: dict[str, object] = {}
    if state:
        params["state"] = _validate(state, _STATES, "state")
    if kind:
        params["kind"] = _validate(kind, _KINDS, "kind")
    if action_required:
        params["action_required"] = True
    if unread:
        params["unread"] = True
    return params


def _filter_params(
    state: str | None,
    kind: str | None,
    action_required: bool,
    unread: bool,
    page: int = 1,
    page_size: int = 25,
) -> dict[str, object]:
    params = _filters(state, kind, action_required, unread)
    params.update({"page": page, "page_size": page_size})
    return params


def _emit_list(params: dict[str, object], output: str) -> None:
    with spinner("Fetching inbox..."):
        data = client.get("/api/v1/inbox", params=params or None)

    if output == "json":
        output_json(data)
        return

    items = data.get("items") or []
    if not items:
        # Silence would be indistinguishable from a broken command.
        rprint("[dim]Nothing in your inbox for that filter.[/dim]")
        return

    table = Table(title=f"Inbox ({data.get('total', len(items))})", show_lines=False, padding=(0, 1))
    table.add_column("#", style="dim", width=3)
    table.add_column("", width=1)  # unread marker
    table.add_column("Kind")
    table.add_column("Title", style="bold", overflow="fold")
    table.add_column("State")
    table.add_column("ID", style="dim")

    # Titles carry user-supplied component names. Rich reads a stray "[...]" as
    # a style tag and raises MarkupError on a bad one, so everything is escaped.
    for i, item in enumerate(items, 1):
        marker = "" if item.get("read") else "●"
        state = item.get("state", "")
        if item.get("action_required") and state == "open":
            state = f"{state} !"
        table.add_row(
            str(i),
            marker,
            esc(item.get("kind", "")),
            esc(item.get("title", "")),
            esc(state),
            esc(str(item.get("id", ""))[:8]),
        )
    console.print(table)

    # Say where this page sits in the result set. Without it a capped list is
    # indistinguishable from the whole inbox, and older items look deleted.
    total = int(data.get("total") or len(items))
    page = int(data.get("page") or 1)
    page_size = int(data.get("page_size") or len(items) or 1)
    first_row = (page - 1) * page_size + 1
    last_row = (page - 1) * page_size + len(items)
    if total > len(items):
        rprint(f"\n[dim]Showing {first_row}-{last_row} of {total}.[/dim]")
        if last_row < total:
            rprint(f"[dim]Next page: [cyan]observal inbox list --page {page + 1}[/cyan][/dim]")

    first_id = items[0].get("id", "")
    rprint(f"\n[dim]Detail: [cyan]observal inbox show {esc(first_id)}[/cyan][/dim]")


@inbox_app.callback(invoke_without_command=True)
def inbox(
    ctx: typer.Context,
    state: str | None = typer.Option(None, "--state", "-s", help=f"Filter by state: {', '.join(_STATES)}"),
    kind: str | None = typer.Option(None, "--kind", "-k", help="Filter by kind"),
    action_required: bool = typer.Option(False, "--action-required", help="Only items needing you to act"),
    unread: bool = typer.Option(False, "--unread", help="Only unread items"),
    page: int = typer.Option(1, "--page", "-p", min=1, help="Page number"),
    page_size: int = typer.Option(25, "--page-size", min=1, max=100, help="Items per page"),
    output: str = typer.Option("table", "--output", "-o", help="Output format: table or json"),
):
    """Show your inbox.

    Examples:

        observal inbox

        observal inbox --action-required

        observal inbox --kind review_requested --output json
    """
    if ctx.invoked_subcommand is None:
        _emit_list(_filter_params(state, kind, action_required, unread, page, page_size), output)


@inbox_app.command(name="list")
def inbox_list(
    state: str | None = typer.Option(None, "--state", "-s", help=f"Filter by state: {', '.join(_STATES)}"),
    kind: str | None = typer.Option(None, "--kind", "-k", help="Filter by kind"),
    action_required: bool = typer.Option(False, "--action-required", help="Only items needing you to act"),
    unread: bool = typer.Option(False, "--unread", help="Only unread items"),
    page: int = typer.Option(1, "--page", "-p", min=1, help="Page number"),
    page_size: int = typer.Option(25, "--page-size", min=1, max=100, help="Items per page"),
    output: str = typer.Option("table", "--output", "-o", help="Output format: table or json"),
):
    """List your inbox items.

    Examples:

        observal inbox list --state open --output json
    """
    _emit_list(_filter_params(state, kind, action_required, unread, page, page_size), output)


@inbox_app.command(name="count")
def inbox_count(
    output: str = typer.Option("table", "--output", "-o", help="Output format: table or json"),
):
    """Show unread and needs-action counts.

    Examples:

        observal inbox count --output json
    """
    with spinner("Fetching counts..."):
        data = client.get("/api/v1/inbox/count")
    if output == "json":
        output_json(data)
        return
    rprint(
        f"[bold]{data.get('unread', 0)}[/bold] unread, "
        f"[bold]{data.get('action_required', 0)}[/bold] needing action, "
        f"[bold]{data.get('open', 0)}[/bold] open"
    )


@inbox_app.command(name="show")
def inbox_show(
    item_id: str = typer.Argument(..., help="Inbox item ID"),
    output: str = typer.Option("table", "--output", "-o", help="Output format: table or json"),
):
    """Show one item with its full action history.

    Examples:

        observal inbox show 6f1c2f3e-...

        observal inbox show 6f1c2f3e-... --output json
    """
    with spinner("Fetching item..."):
        data = client.get(f"/api/v1/inbox/{item_id}")
    if output == "json":
        output_json(data)
        return

    rprint(f"\n[bold]{esc(data.get('title', ''))}[/bold]")
    rprint(f"[dim]{esc(data.get('kind', ''))} · {esc(data.get('state', ''))}[/dim]")
    if data.get("body"):
        rprint(f"\n{esc(data['body'])}")
    if data.get("action_url"):
        rprint(f"\n[dim]Open:[/dim] {esc(data['action_url'])}")
    if data.get("action_command"):
        # Shown for the user to run. The CLI never runs it for them.
        rprint(f"[dim]Run:[/dim]  [cyan]{esc(data['action_command'])}[/cyan]")

    history = data.get("history") or []
    if history:
        rprint("\n[bold]History[/bold]")
        for event in history:
            detail = f" — {esc(event['detail'])}" if event.get("detail") else ""
            rprint(f"  [dim]{esc(event.get('created_at', ''))}[/dim]  {esc(event.get('event', ''))}{detail}")


def _act(item_id: str, action: str, past_tense: str) -> None:
    with spinner(f"Marking {action}..."):
        data = client.post(f"/api/v1/inbox/{item_id}/{action}", {})
    rprint(f"[green]✓ {past_tense}: {esc(data.get('title', item_id))}[/green]")


@inbox_app.command(name="read")
def inbox_read(item_id: str = typer.Argument(..., help="Inbox item ID")):
    """Mark an item read. This does not resolve it — it stays in your open list.

    Examples:

        observal inbox read 6f1c2f3e-...
    """
    _act(item_id, "read", "Marked read")


@inbox_app.command(name="unread")
def inbox_unread(item_id: str = typer.Argument(..., help="Inbox item ID")):
    """Mark an item unread again.

    Examples:

        observal inbox unread 6f1c2f3e-...
    """
    _act(item_id, "unread", "Marked unread")


@inbox_app.command(name="done")
def inbox_done(item_id: str = typer.Argument(..., help="Inbox item ID")):
    """Resolve an item.

    Examples:

        observal inbox done 6f1c2f3e-...
    """
    _act(item_id, "done", "Resolved")


@inbox_app.command(name="dismiss")
def inbox_dismiss(item_id: str = typer.Argument(..., help="Inbox item ID")):
    """Dismiss an item without acting on it.

    Examples:

        observal inbox dismiss 6f1c2f3e-...
    """
    _act(item_id, "dismiss", "Dismissed")


@inbox_app.command(name="reopen")
def inbox_reopen(item_id: str = typer.Argument(..., help="Inbox item ID")):
    """Reopen an item you resolved or dismissed by mistake.

    Examples:

        observal inbox reopen 6f1c2f3e-...
    """
    _act(item_id, "reopen", "Reopened")


@inbox_app.command(name="read-all")
def inbox_read_all(
    state: str | None = typer.Option(None, "--state", "-s", help=f"Filter by state: {', '.join(_STATES)}"),
    kind: str | None = typer.Option(None, "--kind", "-k", help="Filter by kind"),
    action_required: bool = typer.Option(False, "--action-required", help="Only items needing you to act"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation prompt"),
):
    """Mark everything matching the filter as read.

    Scoped to the filter you pass, not to everything: clearing the unread signal
    across an actionable feed hides work you have never looked at.

    Examples:

        observal inbox read-all --kind update_available

        observal inbox read-all --yes
    """
    params = _filters(state, kind, action_required)
    if not yes:
        scope = ", ".join(f"{k}={v}" for k, v in params.items()) or "ALL unread items"
        typer.confirm(f"Mark as read: {scope}?", abort=True)

    # client.post takes no params argument, so the filter rides in the path.
    query = urlencode({k: str(v) for k, v in params.items()})
    path = "/api/v1/inbox/read-all" + (f"?{query}" if query else "")
    with spinner("Marking read..."):
        data = client.post(path, {})
    rprint(f"[green]✓ Marked {data.get('updated', 0)} item(s) read.[/green]")
