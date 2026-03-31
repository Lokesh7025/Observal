import httpx
import typer
from rich import print as rprint
from observal_cli import config


def _client() -> tuple[str, dict]:
    cfg = config.get_or_exit()
    return cfg["server_url"].rstrip("/"), {"X-API-Key": cfg["api_key"]}


def get(path: str, params: dict | None = None) -> dict:
    base, headers = _client()
    try:
        r = httpx.get(f"{base}{path}", headers=headers, params=params, timeout=30)
        r.raise_for_status()
        return r.json()
    except httpx.HTTPStatusError as e:
        detail = e.response.json().get("detail", e.response.text) if e.response.headers.get("content-type", "").startswith("application/json") else e.response.text
        rprint(f"[red]Error {e.response.status_code}: {detail}[/red]")
        raise typer.Exit(code=1)
    except httpx.ConnectError:
        rprint("[red]Connection failed. Is the server running?[/red]")
        raise typer.Exit(code=1)


def post(path: str, json_data: dict | None = None) -> dict:
    base, headers = _client()
    try:
        r = httpx.post(f"{base}{path}", headers=headers, json=json_data, timeout=30)
        r.raise_for_status()
        return r.json()
    except httpx.HTTPStatusError as e:
        detail = e.response.json().get("detail", e.response.text) if e.response.headers.get("content-type", "").startswith("application/json") else e.response.text
        rprint(f"[red]Error {e.response.status_code}: {detail}[/red]")
        raise typer.Exit(code=1)
    except httpx.ConnectError:
        rprint("[red]Connection failed. Is the server running?[/red]")
        raise typer.Exit(code=1)
