"""Command line entry points for the backend.

Three commands, because that is all a desktop shell needs:

``serve``    run the API (what the Electron shell spawns)
``migrate``  bring the database at the configured data root up to head
``version``  print the backend version and the migration head, for diagnostics

The same module is the entry point of the packaged executable, so a machine with no Python
installation can still migrate and serve (Phase 9).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

APP_VERSION = "0.8.0"


def _backend_root() -> Path:
    """The directory that holds ``alembic.ini`` and ``migrations/``.

    Three layouts have to work:

    * source checkout — ``app/backend/src/creatoros/__main__.py`` → ``app/backend``
    * PyInstaller onedir — data files are extracted beside the executable (``_MEIPASS`` for
      onefile, ``sys.executable``'s directory for onedir)
    * an operator override — ``CREATOROS_BACKEND_ROOT``
    """
    override = os.environ.get("CREATOROS_BACKEND_ROOT")
    candidates: list[Path] = []
    if override:
        candidates.append(Path(override))
    bundle = getattr(sys, "_MEIPASS", None)
    if bundle:
        candidates.append(Path(bundle))
    candidates.append(Path(sys.executable).resolve().parent)
    candidates.append(Path(__file__).resolve().parents[2])
    for candidate in candidates:
        if (candidate / "alembic.ini").is_file():
            return candidate
    raise SystemExit(
        "could not locate alembic.ini (looked in: " + ", ".join(str(item) for item in candidates) + ")"
    )


def _alembic_config():
    from alembic.config import Config

    root = _backend_root()
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "migrations"))
    return config


def command_migrate(args: argparse.Namespace) -> int:
    from alembic import command
    from alembic.runtime.migration import MigrationContext

    from .db.session import database_path, get_engine

    config = _alembic_config()
    before = current_revision()
    print(f"[migrate] database: {database_path()}")
    print(f"[migrate] from: {before or '(empty)'}")
    command.upgrade(config, args.revision)

    with get_engine().connect() as connection:
        after = MigrationContext.configure(connection).get_current_revision()
    print(f"[migrate] to:   {after or '(empty)'}")
    if args.json:
        print(json.dumps({"before": before, "after": after, "database": str(database_path())}))
    return 0


def current_revision() -> str | None:
    from alembic.runtime.migration import MigrationContext

    from .db.session import get_engine

    with get_engine().connect() as connection:
        return MigrationContext.configure(connection).get_current_revision()


def head_revision() -> str | None:
    from alembic.script import ScriptDirectory

    return ScriptDirectory.from_config(_alembic_config()).get_current_head()


def command_version(args: argparse.Namespace) -> int:
    from .db.session import database_path

    try:
        current = current_revision()
    except sa_error():  # pragma: no cover - diagnostics must not raise
        current = None
    payload = {
        "version": APP_VERSION,
        "python": sys.version.split()[0],
        "database": str(database_path()),
        "revision": current,
        "head": head_revision(),
        "frozen": bool(getattr(sys, "frozen", False)),
    }
    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        for key, value in payload.items():
            print(f"{key}: {value}")
    return 0


def sa_error():
    import sqlalchemy as sa

    return sa.exc.SQLAlchemyError


def command_serve(args: argparse.Namespace) -> int:
    import uvicorn

    # The application object is imported here rather than passed as "creatoros.main:app": in a
    # frozen build uvicorn's import-string path fails, because PyInstaller only bundles modules
    # that were imported at analysis time, and a string uvicorn resolves at runtime was never one
    # of them.
    from .main import app

    uvicorn.run(
        app,
        host=args.host,
        port=args.port,
        log_level=args.log_level,
        # The shell captures stdout/stderr into its own log; uvicorn's default access log would
        # duplicate every request line there.
        access_log=False,
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="creatoros", description="CreatorOS backend")
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="run the API server")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    serve.add_argument("--log-level", default="warning")
    serve.set_defaults(func=command_serve)

    migrate = sub.add_parser("migrate", help="upgrade the database to head")
    migrate.add_argument("--revision", default="head")
    migrate.add_argument("--json", action="store_true", help="also print a machine-readable summary")
    migrate.set_defaults(func=command_migrate)

    version = sub.add_parser("version", help="print version and migration state")
    version.add_argument("--json", action="store_true")
    version.set_defaults(func=command_version)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
