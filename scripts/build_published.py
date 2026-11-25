"""Build news/_published.json: the day each item went public and the day it last changed.

The feed itself stores only the post date (source.posted_at). The site needs two more dates:
the sitemap lastmod (the day a page appeared or changed) and the Atom feed, which carries only
fresh news. Both come from git history of main, so nothing new has to be stored by hand:

    published  first-parent commit of main that added news/items/<id>.json
    modified   last first-parent commit that changed the item file
    locales    per language: the later of `modified` and the last change of
               news/translations/<lang>/<id>.json (absent when there is no translation)

A file that is in the working tree but not yet in history (a feed pull request being built
locally) gets today's UTC date; so does a file changed in the working tree.

    python scripts/build_published.py          rebuild news/_published.json
    python scripts/build_published.py --check  exit 1 if the committed file is stale

Exit 0 = written / current, 1 = stale (--check), 2 = could not build (no git, shallow clone).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT_NAME = "_published.json"
SCHEMA = "published-v1"
ITEM_PATH = re.compile(r"^news/items/(n-[0-9a-f]{10})\.json$")
TRANSLATION_PATH = re.compile(r"^news/translations/([A-Za-z-]+)/(n-[0-9a-f]{10})\.json$")
DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class BuildError(Exception):
    pass


def git(root: Path, *args: str) -> str:
    env = dict(os.environ, TZ="UTC", LC_ALL="C")
    try:
        done = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True,
                              encoding="utf-8", env=env, timeout=600)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise BuildError(f"git {args[0]}: {exc}") from exc
    if done.returncode != 0:
        raise BuildError(f"git {' '.join(args[:2])}: {done.stderr.strip()}")
    return done.stdout


def history(root: Path) -> tuple[dict[str, str], dict[str, str]]:
    """path -> (newest change day, day it was added), from main's first-parent history."""
    if git(root, "rev-parse", "--is-shallow-repository").strip() == "true":
        raise BuildError("shallow clone: the publish days need full history (actions/checkout fetch-depth: 0)")
    out = git(root, "log", "--first-parent", "--diff-merges=first-parent", "--no-renames",
              "--date=format-local:%Y-%m-%d", "--format=%x00%cd", "--name-only",
              "--", "news/items", "news/translations")
    newest: dict[str, str] = {}
    added: dict[str, str] = {}
    day = None
    for line in out.splitlines():
        if line.startswith("\x00"):
            day = line[1:].strip()
            if not DATE.match(day):
                raise BuildError(f"unexpected commit date {day!r}")
            continue
        path = line.strip()
        if not path or day is None:
            continue
        newest.setdefault(path, day)
        added[path] = day  # log runs newest first, so the last write is the oldest commit
    return newest, added


def worktree_changes(root: Path) -> set[str]:
    out = git(root, "status", "--porcelain", "--no-renames", "-uall", "--", "news/items", "news/translations")
    return {line[3:].strip().strip('"') for line in out.splitlines() if len(line) > 3}


def build(root: Path, today: str | None = None) -> dict:
    today = today or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    newest, added = history(root)
    dirty = worktree_changes(root)
    news = root / "news"
    items: dict[str, dict] = {}
    for path in sorted((news / "items").glob("n-*.json")):
        rel = f"news/items/{path.name}"
        item_id = path.stem
        published = added.get(rel, today)
        modified = today if rel in dirty or rel not in newest else newest[rel]
        items[item_id] = {"published": published, "modified": max(modified, published), "locales": {}}
    for path in sorted((news / "translations").glob("*/n-*.json")):
        rel = f"news/translations/{path.parent.name}/{path.name}"
        entry = items.get(path.stem)
        if entry is None:
            continue
        changed = today if rel in dirty or rel not in newest else newest[rel]
        entry["locales"][path.parent.name] = max(changed, entry["modified"])
    return {"schema": SCHEMA, "items": items}


def render(data: dict) -> bytes:
    return (json.dumps(data, ensure_ascii=False, indent=1, sort_keys=True) + "\n").encode("utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--root", type=Path, default=ROOT)
    args = parser.parse_args(argv)
    root = args.root.resolve()
    try:
        body = render(build(root))
    except BuildError as exc:
        print(f"UNKNOWN: {exc}", file=sys.stderr)
        return 2
    target = root / "news" / OUT_NAME
    if args.check:
        if not target.is_file() or target.read_bytes() != body:
            print(f"{target.relative_to(root)} is stale: run python scripts/build_published.py", file=sys.stderr)
            return 1
        print("publish days are current")
        return 0
    target.write_bytes(body)
    print(f"wrote {target.relative_to(root)}: {len(json.loads(body)['items'])} items")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
