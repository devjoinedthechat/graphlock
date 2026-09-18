"""graphlock command line: lock, check, scan, rules, show."""

from __future__ import annotations

import argparse
import contextlib
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from graphlock import lockfile, report
from graphlock.check import check
from graphlock.findings import Finding, Severity
from graphlock.loader import ConfigError, load_config, load_graph, load_migrations, open_checkpointer
from graphlock.scan import scan
from graphlock.shape import extract_shape

EXIT_OK, EXIT_BREAKING, EXIT_ERROR = 0, 1, 2


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="graphlock",
        description="Catch LangGraph changes that break paused threads before you deploy them.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def common(p: argparse.ArgumentParser) -> None:
        p.add_argument(
            "--graph",
            action="append",
            default=[],
            metavar="NAME=MODULE:ATTR",
            help="a graph to use (repeatable); overrides [tool.graphlock.graphs] in pyproject.toml",
        )
        p.add_argument("--root", type=Path, default=None, help="project root (default: current directory)")

    def migrations(p: argparse.ArgumentParser) -> None:
        group = p.add_mutually_exclusive_group()
        group.add_argument(
            "--migrations", metavar="MODULE:ATTR", help="migrations to apply (overrides pyproject.toml)"
        )
        group.add_argument("--no-migrations", action="store_true", help="ignore configured migrations")

    p = sub.add_parser("lock", help="record the shape of the deployed graphs in the lockfile")
    common(p)

    p = sub.add_parser("check", help="compare the graphs with the lockfile; exit 1 on breaking changes")
    common(p)
    migrations(p)
    p.add_argument("--format", choices=["text", "json", "github"], default="text")
    p.add_argument("--strict", action="store_true", help="treat warnings as breaking")

    p = sub.add_parser("scan", help="report which stored threads the graphs would break")
    common(p)
    migrations(p)
    p.add_argument("--checkpointer", metavar="MODULE:ATTR", help="a checkpointer, or a factory returning one")
    p.add_argument("--sqlite", metavar="PATH", help="a SqliteSaver database file")
    p.add_argument("--postgres", metavar="URL", help="a PostgresSaver connection string")
    p.add_argument("--thread", action="append", default=None, metavar="ID", help="only these thread ids")
    p.add_argument(
        "--no-lock", action="store_true", help="don't read the lockfile (skips interrupt-order checks)"
    )
    p.add_argument("--format", choices=["text", "json"], default="text")

    sub.add_parser("rules", help="list the rules and what LangGraph does in each case")

    p = sub.add_parser("show", help="print the shape of the graphs as JSON")
    common(p)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "rules":
            sys.stdout.write(report.rules_text())
            return EXIT_OK
        if args.command == "lock":
            return _lock(args)
        if args.command == "check":
            return _check(args)
        if args.command == "scan":
            return _scan(args)
        if args.command == "show":
            config = load_config(args.root, args.graph)
            shapes = {n: extract_shape(load_graph(p, config.root)) for n, p in config.graphs.items()}
            sys.stdout.write(json.dumps(shapes, indent=2) + "\n")
            return EXIT_OK
    except ConfigError as exc:
        sys.stderr.write(f"graphlock: {exc}\n")
        return EXIT_ERROR
    return EXIT_ERROR  # pragma: no cover - argparse requires a command


def _migrations_path(args: argparse.Namespace, configured: str | None) -> str | None:
    if args.no_migrations:
        return None
    return args.migrations or configured


def _lock(args: argparse.Namespace) -> int:
    config = load_config(args.root, args.graph)
    shapes = {name: extract_shape(load_graph(path, config.root)) for name, path in config.graphs.items()}
    lockfile.write(config.lockfile, shapes)
    names = ", ".join(sorted(shapes))
    sys.stdout.write(f"wrote {config.lockfile.name} ({names})\n")
    return EXIT_OK


def _check(args: argparse.Namespace) -> int:
    config = load_config(args.root, args.graph)
    if not config.lockfile.exists():
        raise ConfigError(
            f"{config.lockfile.name} not found. Run `graphlock lock` on the code that is deployed now, "
            "and commit the file."
        )
    try:
        locked = lockfile.read(config.lockfile)
    except ValueError as exc:
        raise ConfigError(str(exc)) from exc
    results: dict[str, list[Finding]] = {}
    notes = []
    for name, path in config.graphs.items():
        if name not in locked:
            notes.append(f"{name} is not in {config.lockfile.name} yet; `graphlock lock` adds it.")
            continue
        migrations = load_migrations(_migrations_path(args, config.migrations), name, config.root)
        results[name] = check(locked[name], extract_shape(load_graph(path, config.root)), migrations)
    for name in sorted(set(locked) - set(config.graphs)):
        notes.append(f"{name} is in {config.lockfile.name} but no longer configured.")

    if args.format == "json":
        sys.stdout.write(report.check_json(results, notes))
    elif args.format == "github":
        sys.stdout.write(report.check_github(results, config.lockfile.name))
        sys.stdout.write(report.check_text(results, notes))
    else:
        sys.stdout.write(report.check_text(results, notes))

    def fails(f: Finding) -> bool:
        return f.blocking or (args.strict and f.level is Severity.WARNING and f.handled_by is None)

    return EXIT_BREAKING if any(fails(f) for fs in results.values() for f in fs) else EXIT_OK


def _scan(args: argparse.Namespace) -> int:
    config = load_config(args.root, args.graph)
    locked = {}
    if not args.no_lock and config.lockfile.exists():
        with contextlib.suppress(ValueError):
            locked = lockfile.read(config.lockfile)
    results = {}
    with contextlib.ExitStack() as stack:
        saver = open_checkpointer(
            stack, factory=args.checkpointer, sqlite=args.sqlite, postgres=args.postgres, root=config.root
        )
        for name, path in config.graphs.items():
            graph = load_graph(path, config.root)
            migrations = load_migrations(_migrations_path(args, config.migrations), name, config.root)
            results[name] = scan(
                graph, saver, migrations=migrations, lock=locked.get(name), thread_ids=args.thread
            )
    if args.format == "json":
        sys.stdout.write(report.scan_json(results))
    else:
        for name, result in results.items():
            sys.stdout.write(report.scan_text(name, result))
    return EXIT_BREAKING if any(r.blocking for r in results.values()) else EXIT_OK


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
