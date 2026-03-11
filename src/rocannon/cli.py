import logging
from pathlib import Path

import click

from rocannon.config import Config, load_profile
from rocannon.server import create_server


@click.group()
def cli():
    """Rocannon — Ansible modules as MCP tools."""


@cli.command()
@click.option(
    "--inventory",
    "inventories",
    multiple=True,
    type=click.Path(exists=True, path_type=Path),
    help="Inventory file (repeatable).",
)
@click.option(
    "--modules",
    "modules",
    multiple=True,
    type=str,
    help="Module, collection, or namespace (repeatable).",
)
@click.option(
    "--profile",
    "profile",
    type=click.Path(exists=True, path_type=Path),
    default=None,
    help="YAML profile file (alternative to --inventory/--modules).",
)
@click.option(
    "--transport",
    type=click.Choice(["stdio", "http"]),
    default="stdio",
    help="MCP transport [default: stdio].",
)
@click.option(
    "--log-level",
    type=click.Choice(["DEBUG", "INFO", "WARNING", "ERROR"], case_sensitive=False),
    default="INFO",
    help="Logging level [default: INFO].",
)
def serve(
    inventories: tuple[Path, ...],
    modules: tuple[str, ...],
    profile: Path | None,
    transport: str,
    log_level: str,
):
    """Start the Rocannon MCP server."""
    logging.basicConfig(
        level=getattr(logging, log_level.upper()),
        format="%(name)s %(levelname)s %(message)s",
    )

    has_flags = bool(inventories or modules)

    if profile and has_flags:
        raise click.UsageError("--profile and --inventory/--modules are mutually exclusive.")

    if profile:
        config = load_profile(profile)
        config.transport = transport
    elif has_flags:
        config = Config(
            inventories=list(inventories),
            modules=list(modules),
            transport=transport,
        )
    else:
        raise click.UsageError("Provide either --profile or at least --inventory and --modules.")

    server = create_server(config)
    server.run(transport=transport)


def main():
    """CLI entrypoint."""
    cli()


if __name__ == "__main__":
    main()
