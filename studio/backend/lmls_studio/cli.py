"""Command line entry point for the Livesub Studio backend."""
from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer

app = typer.Typer(add_completion=False)


@app.callback()
def main() -> None:
    """Livesub Studio backend: local graph editor, subtitle monitor, and diagnostics."""


@app.command()
def serve(
    config: Optional[Path] = typer.Option(None, "--config", "-c", help="Initial graph TOML."),
    port: int = typer.Option(8080, min=1, max=65535, help="Local web port."),
    media_root: Path = typer.Option(Path.cwd(), help="Directory containing playable media files."),
) -> None:
    """Open the local graph editor, subtitle monitor, and live diagnostics."""
    try:
        from aiohttp import web as aiohttp_web
        from .server import create_app
    except ImportError as exc:
        raise typer.BadParameter("install studio dependencies with: pip install -e studio/backend") from exc
    typer.echo(f"Livesub Studio: http://127.0.0.1:{port}")
    aiohttp_web.run_app(create_app(config, media_root), host="127.0.0.1", port=port)


def app_main() -> None:
    app()


if __name__ == "__main__":
    app()