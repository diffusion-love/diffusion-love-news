"""validate_publish_paths.py -- a feed change loses nothing an item is connected to.

Crossweft validator (python -m crossweft validators). It runs scripts/publish_paths.py in `pr`
mode: the feed tree against PUBLISH_CHECK_BASE (CI sets it to the PR base or the commit before
the push; default = merge-base with origin/main). Rules: pipeline repo docs/publish-contract.md.

--self-test builds a tiny feed in a temporary git repository and requires every planted defect
(a dropped predecessor, a changed post date, an index date that differs from the post date, a
removed translation) to fail and the untouched feed to pass.
"""
from __future__ import annotations

import copy
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve()
ROOT = next((p for p in HERE.parents if (p / "crossweft.json").is_file()), None)
if ROOT is None:
    sys.exit("[ERR] no crossweft.json above this validator -- misconfigured")
SPEC = importlib.util.spec_from_file_location("publish_paths", ROOT / "scripts" / "publish_paths.py")
pp = importlib.util.module_from_spec(SPEC)
sys.modules["publish_paths"] = pp  # dataclasses resolve annotations through sys.modules
SPEC.loader.exec_module(pp)


def _write(root: Path, rel: str, value) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=1), encoding="utf-8")


def _item(item_id: str, posted: str, preds: list[str]) -> dict:
    return {
        "schema_version": "1.4", "id": item_id, "source": {"posted_at": posted},
        "project": {"family_slug": "demo"}, "organization": {"slug": "acme", "display": "Acme"},
        "tags": ["family:demo", "organization:acme"],
        "development": {"scope": "project", "thread_slug": "demo-line", "predecessor_ids": preds},
        "knowledge": {"article_url": "https://example.invalid/demo/"},
    }


def _feed(root: Path, items: list[dict], *, index_dates: dict | None = None, drop_lang: str | None = None) -> None:
    ids = [i["id"] for i in items]
    for item in items:
        _write(root, f"news/items/{item['id']}.json", item)
    _write(root, "news/_index.json",
           [{"id": i["id"], "date": (index_dates or {}).get(i["id"], i["source"]["posted_at"])} for i in items])
    _write(root, "news/_projects.json", {"demo": {"news_ids": ids}})
    _write(root, "news/_organizations.json", {"acme": {"news_ids": ids}})
    _write(root, "news/_published.json", {"schema": "published-v1", "items": {
        i["id"]: {"published": i["source"]["posted_at"], "modified": i["source"]["posted_at"],
                  "locales": {lang: i["source"]["posted_at"] for lang in pp.LOCALES}} for i in items}})
    for lang in pp.LOCALES:
        keep = [i for i in ids if not (lang == drop_lang and i == ids[-1])]
        _write(root, f"news/translations/{lang}/_index.json", {i: {"title": i} for i in keep})
        for i in ids:
            path = root / f"news/translations/{lang}/{i}.json"
            if i in keep:
                _write(root, f"news/translations/{lang}/{i}.json", {"title": i})
            elif path.exists():
                path.unlink()


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)


def self_test() -> int:
    a = _item("n-aaaaaaaaaa", "2026-07-01", [])
    b = _item("n-bbbbbbbbbb", "2026-07-02", ["n-aaaaaaaaaa"])
    cases = {
        "untouched": (lambda r: None, set()),
        "touched, still whole": (lambda r: _feed(r, [a, dict(b, title="edited")]), set()),
        "dropped predecessor": (lambda r: _feed(r, [a, _with(b, ["development", "predecessor_ids"], [])]),
                                {"L2.predecessor_lost"}),
        "changed post date": (lambda r: _feed(r, [a, _with(b, ["source", "posted_at"], "2026-11-25")]),
                              {"L1.date_changed"}),
        "index date != post date": (lambda r: _feed(r, [a, b], index_dates={b["id"]: "2026-11-25"}),
                                    {"P1.feed_date"}),
        "removed translation": (lambda r: _feed(r, [a, b], drop_lang="ko"), {"L5.locale_lost"}),
        "dropped article": (lambda r: _feed(r, [a, _with(b, ["knowledge"], None)]), {"L3.article_lost", "P3.article"}),
    }
    bad = []
    for name, (mutate, expected) in cases.items():
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _feed(root, [a, b])
            _git(root, "init", "-q")
            _git(root, "-c", "user.name=t", "-c", "user.email=t@t", "add", "-A")
            _git(root, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "base")
            mutate(root)
            report = pp.run_pr(root, "HEAD", offline=True)
            got = {line.split()[1] for line in report.fails}
            ok = (report.code() == 0) if not expected else (report.code() == 1 and expected <= got)
            if not ok:
                bad.append(f"{name}: expected {sorted(expected) or 'pass'}, got code {report.code()} {sorted(got)}")
    for line in bad:
        print(f"[SELF-TEST FAIL] {line}")
    if bad:
        return 1
    print(f"SELF-TEST: checks={len(cases)}")
    return 0


def _with(item: dict, path: list[str], value) -> dict:
    out = copy.deepcopy(item)
    cur = out
    for key in path[:-1]:
        cur = cur[key]
    cur[path[-1]] = value
    return out


def main(argv: list[str]) -> int:
    if "--self-test" in argv:
        return self_test()
    base = os.environ.get("PUBLISH_CHECK_BASE") or pp.default_base(ROOT)
    report = pp.run_pr(ROOT, base, offline=os.environ.get("PUBLISH_CHECK_OFFLINE") == "1")
    report.print()
    return report.code()


sys.exit(main(sys.argv[1:]))
