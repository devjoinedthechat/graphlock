"""graphlock command line: lock, check, scan, rules, show."""

from __future__ import annotations

import argparse
import contextlib
import json
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

from graphlock import lockfile, report
from graphlock.check import check, check_rollback
from graphlock.findings import Finding, Severity
from graphlock.loader import (
    Config,
    ConfigError,
    load_config,
    load_graph,
    load_migrations,
    open_checkpointer,
)
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
        p.add_argument(
            "--lockfile",
            type=Path,
            default=None,
            help="the lockfile to use, e.g. one per environment (default: [tool.graphlock] lockfile)",
        )

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
    p.add_argument(
        "--reverse",
        action="store_true",
        help="report what rolling back to the locked version would break, once this code has run",
    )

    p = sub.add_parser("scan", help="report which stored threads the graphs would break")
    common(p)
    migrations(p)
    p.add_argument("--checkpointer", metavar="MODULE:ATTR", help="a checkpointer, or a factory returning one")
    p.add_argument("--sqlite", metavar="PATH", help="a SqliteSaver database file")
    p.add_argument("--postgres", metavar="URL", help="a PostgresSaver connection string")
    p.add_argument("--thread", action="append", default=None, metavar="ID", help="only these thread ids")
    p.add_argument("--thread-prefix", metavar="PREFIX", help="only threads whose id starts with PREFIX")
    p.add_argument("--sample", type=int, metavar="N", help="scan N threads chosen at random (reproducibly)")
    p.add_argument("--seed", type=int, default=0, help="the seed for --sample (default 0)")
    p.add_argument("--progress", action="store_true", help="report progress on stderr")
    p.add_argument(
        "--where",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="only threads whose checkpoint metadata has KEY=VALUE (repeatable)",
    )
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
            config = _config(args)
            shapes = {n: extract_shape(load_graph(p, config.root)) for n, p in config.graphs.items()}
            sys.stdout.write(json.dumps(shapes, indent=2) + "\n")
            return EXIT_OK
    except ConfigError as exc:
        sys.stderr.write(f"graphlock: {exc}\n")
        return EXIT_ERROR
    return EXIT_ERROR  # pragma: no cover - argparse requires a command


def _config(args: argparse.Namespace) -> Config:
    config = load_config(args.root, args.graph)
    if getattr(args, "lockfile", None) is not None:
        config.lockfile = args.lockfile if args.lockfile.is_absolute() else config.root / args.lockfile
    return config


def _progress(name: str) -> Callable[[int, int], None]:
    def report(done: int, total: int) -> None:
        if done == total or done % 500 == 0:
            sys.stderr.write(f"\r{name}: read {done:,} of {total:,} checkpoints")
            if done == total:
                sys.stderr.write("\n")
            sys.stderr.flush()

    return report


def _migrations_path(args: argparse.Namespace, configured: str | None) -> str | None:
    if args.no_migrations:
        return None
    return args.migrations or configured


def _lock(args: argparse.Namespace) -> int:
    config = _config(args)
    shapes = {name: extract_shape(load_graph(path, config.root)) for name, path in config.graphs.items()}
    lockfile.write(config.lockfile, shapes)
    names = ", ".join(sorted(shapes))
    sys.stdout.write(f"wrote {config.lockfile.name} ({names})\n")
    return EXIT_OK


def _check(args: argparse.Namespace) -> int:
    config = _config(args)
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
        current = extract_shape(load_graph(path, config.root))
        if args.reverse:
            results[name] = check_rollback(current, locked[name])
        else:
            migrations = load_migrations(_migrations_path(args, config.migrations), name, config.root)
            results[name] = check(locked[name], current, migrations)
    for name in sorted(set(locked) - set(config.graphs)):
        notes.append(f"{name} is in {config.lockfile.name} but no longer configured.")

    header = None
    if args.reverse:
        header = (
            f"Rollback check: what threads that ran on this code would hit if you rolled back to "
            f"{config.lockfile.name}. Migrations don't run backwards; a repair for a rollback has to "
            "ship in the code you roll back to."
        )
    if args.format == "json":
        sys.stdout.write(report.check_json(results, notes, header))
    elif args.format == "github":
        sys.stdout.write(report.check_github(results, config.lockfile.name))
        sys.stdout.write(report.check_text(results, notes, header))
    else:
        sys.stdout.write(report.check_text(results, notes, header))

    def fails(f: Finding) -> bool:
        return f.blocking or (args.strict and f.level is Severity.WARNING and f.handled_by is None)

    return EXIT_BREAKING if any(fails(f) for fs in results.values() for f in fs) else EXIT_OK


def _scan(args: argparse.Namespace) -> int:
    config = _config(args)
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
            configured = config.scan.get(name, {})
            where = dict(configured.get("where", {}))
            for pair in args.where:
                key, sep, value = pair.partition("=")
                if not sep:
                    raise ConfigError(f"--where takes KEY=VALUE, got {pair!r}")
                where[key] = value
            results[name] = scan(
                graph,
                saver,
                migrations=migrations,
                lock=locked.get(name),
                thread_ids=args.thread,
                thread_prefix=args.thread_prefix or configured.get("thread_prefix"),
                where=where or None,
                sample=args.sample,
                seed=args.seed,
                on_progress=_progress(name) if args.progress else None,
            )
    if args.format == "json":
        sys.stdout.write(report.scan_json(results))
    else:
        for name, result in results.items():
            sys.stdout.write(report.scan_text(name, result))
    return EXIT_BREAKING if any(r.blocking for r in results.values()) else EXIT_OK


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
