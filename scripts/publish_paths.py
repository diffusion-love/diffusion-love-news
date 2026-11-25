"""Publish-path check: nothing a news item is connected to may be lost on the way to the site.

The contract (one invariant per id) is documented in the pipeline repository,
docs/publish-contract.md. This file is its executable half for the public feed.

    pr    before publishing: the feed tree at HEAD against a base commit (the feed PR's base,
          or the commit before a push). Offline rules over every item, loss rules over every
          item the change touched, live knowledge-base links for touched items.
    live  after publishing: the live site serves the feed commit, and for each touched item its
          pages, head tags, sitemap rows, Atom feeds, MCP and the knowledge base agree.

Exit 0 = every invariant held, 1 = something is lost or wrong, 2 = a check could not run and
nothing failed. A run that checked no item is 2, not 0.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
LOCALES = ("de", "es", "fr", "ko", "ru", "zh-CN")
# The site serves Russian at the root and the other languages under a lowercase prefix.
SITE_PREFIX = {"en": "/en", "ru": "", "de": "/de", "es": "/es", "fr": "/fr", "ko": "/ko", "zh-CN": "/zh-cn"}
HREFLANG = {"en": "en", "ru": "ru", "de": "de", "es": "es", "fr": "fr", "ko": "ko", "zh-CN": "zh-CN"}
# Owner 2026-11-25: feed.xml is the feed of fresh news; a backfilled item keeps its post date and
# stays out. Measured on main 2026-11-25 (scripts/build_published.py): 130 items reached the
# feed 0-2 days after the post, the publish stall of 22-25.11 added 3-4 days; backfilled items lag
# 15+ days. Seven days keeps a stalled fresh item and excludes every backfilled one.
FRESH_DAYS = 7
# translate-live runs on the next publish tick after release-live; a stalled pipeline delayed it
# by up to three days (22-25.11). After this many days a missing language page is a loss.
LOCALE_GRACE_DAYS = 3
ALLOWED_LOSSES = "checks/allowed-losses.json"
USER_AGENT = "diffusion-love-publish-check/1"
SITE = "https://diffusion.love"
CDN = "https://cdn.jsdelivr.net/gh/diffusion-love/diffusion-love-news@"


@dataclass
class Report:
    fails: list[str] = field(default_factory=list)
    unknown: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    checked: int = 0
    files: int = 0

    def fail(self, inv: str, item: str, msg: str) -> None:
        self.fails.append(f"[FAIL] {inv} {item}: {msg}")

    def unk(self, inv: str, item: str, msg: str) -> None:
        self.unknown.append(f"[UNKNOWN] {inv} {item}: {msg}")

    def code(self) -> int:
        if self.fails:
            return 1
        if self.unknown or self.checked == 0:
            return 2
        return 0

    def print(self) -> None:
        for line in self.fails + self.unknown + self.notes:
            print(line)
        print(f"SCANNED: files={self.files} items={self.checked}")
        print(f"publish-paths: {len(self.fails)} lost/wrong, {len(self.unknown)} not checked")


class Tree:
    """The feed as seen at one revision: either the working tree or `git show <rev>:<path>`."""

    def __init__(self, root: Path, rev: str | None = None):
        self.root, self.rev = root, rev

    def read(self, rel: str) -> Any | None:
        if self.rev is None:
            path = self.root / rel
            return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None
        done = subprocess.run(["git", "-C", str(self.root), "show", f"{self.rev}:{rel}"],
                              capture_output=True, encoding="utf-8", timeout=120)
        if done.returncode != 0:
            return None
        return json.loads(done.stdout)


def git(root: Path, *args: str) -> str:
    done = subprocess.run(["git", "-C", str(root), *args], capture_output=True, encoding="utf-8", timeout=600)
    if done.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {done.stderr.strip()}")
    return done.stdout


def changed_ids(root: Path, base: str) -> set[str]:
    out = git(root, "diff", "--name-only", "--no-renames", base, "--", "news/items", "news/translations")
    ids = set()
    for line in out.splitlines():
        m = re.search(r"(n-[0-9a-f]{10})\.json$", line.strip())
        if m:
            ids.add(m.group(1))
    return ids


def posted(item: dict) -> str | None:
    return (item.get("source") or {}).get("posted_at")


def thread_of(item: dict) -> str | None:
    dev = item.get("development")
    return dev.get("thread_slug") if isinstance(dev, dict) else None


def preds_of(item: dict) -> list[str]:
    dev = item.get("development")
    return list(dev.get("predecessor_ids") or []) if isinstance(dev, dict) else []


def article_of(item: dict) -> str | None:
    k = item.get("knowledge")
    return k.get("article_url") if isinstance(k, dict) else None


def family_of(item: dict) -> str | None:
    p = item.get("project")
    return p.get("family_slug") if isinstance(p, dict) else None


def org_of(item: dict) -> str | None:
    o = item.get("organization")
    return o.get("slug") if isinstance(o, dict) else None


def reachable(start: list[str], items: dict[str, dict]) -> set[str]:
    seen, stack = set(), list(start)
    while stack:
        cur = stack.pop()
        if cur in seen or cur not in items:
            continue
        seen.add(cur)
        stack.extend(preds_of(items[cur]))
    return seen


def is_fresh(posted_at: str, published: str) -> bool:
    return (date.fromisoformat(published) - date.fromisoformat(posted_at)).days <= FRESH_DAYS


def load_allowed(root: Path, report: Report) -> dict[tuple[str, str], str]:
    path = root / ALLOWED_LOSSES
    if not path.is_file():
        return {}
    allowed = {}
    for entry in json.loads(path.read_text(encoding="utf-8")):
        if not entry.get("reason"):
            report.fail("P0.allowlist", entry.get("id", "?"), "an allowed loss needs a reason")
            continue
        allowed[(entry["id"], entry["check"])] = entry["reason"]
    return allowed


# ---------------------------------------------------------------- pr mode


def check_item(item_id: str, head: dict, items: dict[str, dict], indexes: dict, report: Report,
               strict: bool) -> None:
    """Rules that hold for one item at HEAD. strict = the item is new or changed in this change."""
    p = posted(head)
    entry = indexes["index"].get(item_id)
    if entry is None:
        report.fail("P1.feed_entry", item_id, "not in news/_index.json")
    elif entry.get("date") != p:
        report.fail("P1.feed_date", item_id, f"_index.json date {entry.get('date')} != source.posted_at {p}")
    thread = thread_of(head)
    if strict and head.get("schema_version") == "1.4" and not thread:
        report.fail("P2.thread", item_id, "no development.thread_slug")
    for pred in preds_of(head):
        other = items.get(pred)
        if other is None:
            report.fail("P2.predecessor", item_id, f"predecessor {pred} does not exist")
        elif (posted(other) or "") > (p or ""):
            report.fail("P2.predecessor", item_id, f"predecessor {pred} is later ({posted(other)} > {p})")
    if strict and thread and not article_of(head):
        report.fail("P3.article", item_id, "thread set but no knowledge.article_url")
    family = family_of(head)
    if family:
        if f"family:{family}" not in head.get("tags", []):
            report.fail("P4.tags", item_id, f"tag family:{family} missing")
        if item_id not in (indexes["projects"].get(family) or {}).get("news_ids", []):
            report.fail("P4.project", item_id, f"not listed in _projects.json[{family}]")
    org = org_of(head)
    if org:
        if f"organization:{org}" not in head.get("tags", []):
            report.fail("P4.tags", item_id, f"tag organization:{org} missing")
        if item_id not in (indexes["organizations"].get(org) or {}).get("news_ids", []):
            report.fail("P4.organization", item_id, f"not listed in _organizations.json[{org}]")
    if strict:
        # Translations arrive in a later feed change (pipeline stage translate-live runs after
        # release-live), so a feed PR may not require them; live mode does, after LOCALE_GRACE_DAYS.
        missing = [lang for lang in LOCALES if item_id not in indexes["translations"][lang]]
        if missing:
            report.notes.append(f"[INFO] P5.locales {item_id}: translations still to come: {', '.join(missing)}")
    pub = indexes["published"].get(item_id)
    if pub is None:
        report.fail("P6.published", item_id, "no row in news/_published.json (run scripts/build_published.py)")
    elif p:
        if pub["modified"] < pub["published"] or any(v < pub["modified"] for v in pub["locales"].values()):
            report.fail("P6.published", item_id, f"dates out of order: {pub}")
        if strict:
            where = "feed.xml" if is_fresh(p, pub["published"]) else "archive only (backfill, not in feed.xml)"
            report.notes.append(f"[INFO] P8.feed {item_id}: posted {p}, published {pub['published']} -> {where}")


def check_losses(item_id: str, base: dict | None, head: dict | None, items: dict[str, dict],
                 base_translations: set[str], head_translations: set[str], report: Report,
                 allowed: dict, used: set) -> None:
    """Rules comparing an item before and after the change: nothing it had may disappear."""
    if base is None:
        return

    def lost(check: str, msg: str) -> None:
        if (item_id, check) in allowed:
            used.add((item_id, check))
            report.notes.append(f"[ALLOWED] {check} {item_id}: {allowed[(item_id, check)]}")
        else:
            report.fail(check, item_id, msg)

    if head is None:
        lost("L0.item_removed", "the item file was removed")
        return
    if posted(base) != posted(head):
        lost("L1.date_changed", f"post date changed {posted(base)} -> {posted(head)}")
    if thread_of(base) and not thread_of(head):
        lost("L2.thread_lost", f"thread {thread_of(base)} dropped")
    gone = set(preds_of(base)) - reachable(preds_of(head), items)
    if gone:
        lost("L2.predecessor_lost", f"predecessor(s) {sorted(gone)} no longer reachable")
    if article_of(base) and not article_of(head):
        lost("L3.article_lost", f"knowledge article {article_of(base)} dropped")
    for prefix in ("family:", "organization:"):
        had = [t for t in base.get("tags", []) if t.startswith(prefix)]
        has = [t for t in head.get("tags", []) if t.startswith(prefix)]
        if had and not has:
            lost("L4.tag_lost", f"tags {had} dropped")
    if family_of(base) and not family_of(head):
        lost("L4.project_lost", f"project {family_of(base)} dropped")
    if org_of(base) and not org_of(head):
        lost("L4.organization_lost", f"organization {org_of(base)} dropped")
    for lang in sorted(base_translations - head_translations):
        lost("L5.locale_lost", f"{lang} translation removed")


def load_indexes(tree: Tree) -> dict:
    return {
        "index": {e["id"]: e for e in tree.read("news/_index.json") or []},
        "projects": tree.read("news/_projects.json") or {},
        "organizations": tree.read("news/_organizations.json") or {},
        "published": (tree.read("news/_published.json") or {}).get("items", {}),
        "translations": {lang: set((tree.read(f"news/translations/{lang}/_index.json") or {}).keys())
                         for lang in LOCALES},
    }


def fetch(url: str, *, data: bytes | None = None, timeout: int = 30) -> tuple[int, str]:
    headers = {"User-Agent": USER_AGENT}
    if data is not None:
        headers.update({"Content-Type": "application/json", "Accept": "application/json, text/event-stream"})
    req = urllib.request.Request(url, data=data, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, ""


def check_kb_links(touched: list[tuple[str, dict]], report: Report, get: Callable = fetch) -> None:
    urls = sorted({article_of(item) for _, item in touched if article_of(item)})
    if not urls:
        return
    try:
        st, full = get("https://happyin.space/llms-full.txt", timeout=120)
    except OSError as exc:
        st, full = 0, ""
        report.unk("G2.kb_llms", "-", f"llms-full.txt: {exc}")
    for item_id, item in touched:
        url = article_of(item)
        if not url:
            continue
        try:
            code, _ = get(url)
        except OSError as exc:
            report.unk("P3.article_live", item_id, f"{url}: {exc}")
            continue
        if code != 200:
            report.fail("P3.article_live", item_id, f"{url} answered {code}")
        if st == 200 and url.rstrip("/") not in full:
            report.fail("G2.kb_llms", item_id, f"{url} is not in happyin.space/llms-full.txt")


def run_pr(root: Path, base: str, *, offline: bool = False, get: Callable = fetch) -> Report:
    report = Report()
    head_tree, base_tree = Tree(root), Tree(root, base)
    item_files = sorted((root / "news" / "items").glob("n-*.json"))
    items = {p.stem: json.loads(p.read_text(encoding="utf-8")) for p in item_files}
    report.files = len(item_files) + 5
    indexes = load_indexes(head_tree)
    try:
        touched = changed_ids(root, base)
    except RuntimeError as exc:
        report.unk("P0.base", "-", str(exc))
        return report
    base_translations = {lang: set((base_tree.read(f"news/translations/{lang}/_index.json") or {}).keys())
                         for lang in LOCALES}
    allowed, used = load_allowed(root, report), set()
    for item_id, item in items.items():
        check_item(item_id, item, items, indexes, report, strict=item_id in touched)
        report.checked += 1
    for item_id in sorted(touched):
        base_item = base_tree.read(f"news/items/{item_id}.json")
        bt = {lang for lang in LOCALES if item_id in base_translations[lang]}
        ht = {lang for lang in LOCALES if item_id in indexes["translations"][lang]}
        check_losses(item_id, base_item, items.get(item_id), items, bt, ht, report, allowed, used)
    for key in sorted(set(allowed) - used):
        report.fail("P0.allowlist", key[0], f"allowed loss {key[1]} matched nothing: remove the entry")
    if not offline:
        check_kb_links([(i, items[i]) for i in sorted(touched) if i in items], report, get)
    report.notes.append(f"[INFO] base {base}: {len(touched)} item(s) touched")
    return report


# ---------------------------------------------------------------- live mode


def page_url(lang: str, item_id: str) -> str:
    return f"{SITE}{SITE_PREFIX[lang]}/item/{item_id}"


def head_facts(html: str) -> dict:
    canonical = re.search(r'<link rel="canonical" href="([^"]+)"', html)
    alternates = dict((m.group(1), m.group(2)) for m in
                      re.finditer(r'<link rel="alternate" hreflang="([^"]+)" href="([^"]+)"', html))
    published = re.search(r'"datePublished":"([^"]+)"', html)
    links = set(re.findall(r'href="(?:/[a-z-]+)?/item/(n-[0-9a-f]{10})"', html))
    return {"canonical": canonical.group(1) if canonical else None, "alternates": alternates,
            "news_article": '"@type":"NewsArticle"' in html,
            "date_published": published.group(1) if published else None, "item_links": links}


def run_live(root: Path, ids: list[str], sha: str | None, *, wait_min: int = 0, get: Callable = fetch) -> Report:
    report = Report()
    today = datetime.now(timezone.utc).date()
    allowed = load_allowed(root, report)
    items = {}
    for item_id in ids:
        path = root / "news" / "items" / f"{item_id}.json"
        if path.is_file():
            items[item_id] = json.loads(path.read_text(encoding="utf-8"))
    published = (Tree(root).read("news/_published.json") or {}).get("items", {})
    translations = {lang: set((Tree(root).read(f"news/translations/{lang}/_index.json") or {}).keys())
                    for lang in LOCALES}
    deadline = time.time() + wait_min * 60
    while True:
        try:
            st, body = get(f"{SITE}/api/news-source")
            served = json.loads(body).get("sha") if st == 200 else None
        except (OSError, ValueError):
            served = None
        if sha is None or served == sha or time.time() >= deadline:
            break
        time.sleep(60)
    if sha and served != sha:
        report.unk("S0.served_sha", "-", f"site serves {served}, expected {sha}")
        return report
    try:
        _, sitemap = get(f"{SITE}/sitemap.xml", timeout=120)
        feeds = {lang: get(f"{SITE}{SITE_PREFIX[lang]}/feed.xml")[1] for lang in ("en", "ru")}
    except OSError as exc:
        report.unk("S0.fetch", "-", str(exc))
        return report
    lastmod = dict(re.findall(r"<loc>([^<]+)</loc><lastmod>([^<]+)</lastmod>", sitemap))
    report.files = 3
    touched = []
    for item_id, item in sorted(items.items()):
        p, pub = posted(item), published.get(item_id)
        langs = ["en"] + [lang for lang in LOCALES if item_id in translations[lang]]
        if pub and (today - date.fromisoformat(pub["published"])).days > LOCALE_GRACE_DAYS:
            for lang in LOCALES:
                if lang not in langs and (item_id, "S9.locales") not in allowed:
                    report.fail("S9.locales", item_id, f"published {pub['published']}, still no {lang} page")
        for lang in langs:
            url = page_url(lang, item_id)
            try:
                code, html = get(url)
            except OSError as exc:
                report.unk("P5.page", item_id, f"{url}: {exc}")
                continue
            report.files += 1
            if code != 200:
                report.fail("P5.page", item_id, f"{url} answered {code}")
                continue
            f = head_facts(html)
            if not f["news_article"]:
                report.fail("S1.news_article", item_id, f"{url}: no NewsArticle JSON-LD")
            if not (f["date_published"] or "").startswith(p or "?"):
                report.fail("S2.date_published", item_id, f"{url}: datePublished {f['date_published']} != {p}")
            if f["canonical"] != url:
                report.fail("S3.canonical", item_id, f"{url}: canonical {f['canonical']}")
            for other in langs:
                if f["alternates"].get(HREFLANG[other]) != page_url(other, item_id):
                    report.fail("S4.hreflang", item_id, f"{url}: hreflang {other} missing or wrong")
            for pred in preds_of(item):
                if pred not in f["item_links"]:
                    report.fail("S7.item_links", item_id, f"{url}: no link to predecessor {pred}")
            want = None
            if pub:
                want = pub["modified"] if lang == "en" else pub["locales"].get(lang, pub["modified"])
            if url not in lastmod:
                report.fail("S5.sitemap", item_id, f"{url} not in sitemap.xml")
            elif want and lastmod[url] != want:
                report.fail("S6.lastmod", item_id, f"{url}: lastmod {lastmod[url]} != publish/change day {want}")
        if pub and p:
            fresh = is_fresh(p, pub["published"])
            for lang, feed in feeds.items():
                present = f"/item/{item_id}\"" in feed
                if not fresh and present:
                    report.fail("S8.feed_fresh_only", item_id, f"backfilled item is in {lang} feed.xml")
                oldest = min(re.findall(r"<published>(\d{4}-\d{2}-\d{2})", feed) or ["9999"])
                if fresh and not present and (p or "") > oldest:
                    report.fail("S8.feed_fresh", item_id, f"fresh item missing from {lang} feed.xml")
        if sha:
            code, _ = get(f"{CDN}{sha}/news/items/{item_id}.json")
            if code != 200:
                report.fail("G1.json", item_id, f"feed JSON at {sha[:9]} answered {code}")
        touched.append((item_id, item))
        report.checked += 1
    check_mcp(touched, report, get)
    check_kb_links(touched, report, get)
    return report


def check_mcp(touched: list[tuple[str, dict]], report: Report, get: Callable) -> None:
    """An agent asking MCP for the item's project (or organization) since its post date finds it."""
    for item_id, item in touched:
        since = posted(item)
        if family_of(item):
            call = {"name": "project_updates", "arguments": {"family_slug": family_of(item), "since": since, "limit": 50}}
        elif org_of(item):
            call = {"name": "news_updates", "arguments": {"orgs": [org_of(item)], "since": since, "limit": 50}}
        else:
            continue
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": call}).encode()
        try:
            code, text = get(f"{SITE}/mcp", data=body)
        except OSError as exc:
            report.unk("G1.mcp", item_id, str(exc))
            continue
        if code != 200 or item_id not in text:
            report.fail("G1.mcp", item_id, f"MCP {call['name']} since {since} did not return the item ({code})")


# ---------------------------------------------------------------- cli


def default_base(root: Path) -> str:
    env = os.environ.get("PUBLISH_CHECK_BASE")
    if env:
        return env
    try:
        return git(root, "merge-base", "HEAD", "origin/main").strip()
    except RuntimeError:
        return "HEAD"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="mode", required=True)
    pr = sub.add_parser("pr")
    pr.add_argument("--base")
    pr.add_argument("--offline", action="store_true", help="skip the live knowledge-base probes")
    live = sub.add_parser("live")
    live.add_argument("--sha", help="feed commit the site must serve")
    live.add_argument("--base", help="check the items changed between --base and HEAD")
    live.add_argument("--ids", nargs="*", default=[])
    live.add_argument("--recent-days", type=int, default=0,
                      help="also check every item published or changed in the last N days (_published.json)")
    live.add_argument("--wait-min", type=int, default=0)
    parser.add_argument("--root", type=Path, default=ROOT)
    args = parser.parse_args(argv)
    root = args.root.resolve()
    if args.mode == "pr":
        report = run_pr(root, args.base or default_base(root), offline=args.offline)
    else:
        ids = list(args.ids)
        if args.base:
            ids += sorted(changed_ids(root, args.base))
        if args.recent_days:
            since = (datetime.now(timezone.utc).date() - timedelta(days=args.recent_days)).isoformat()
            ledger = (Tree(root).read("news/_published.json") or {}).get("items", {})
            ids += [i for i, e in ledger.items() if max([e["modified"], *e["locales"].values()]) >= since]
        report = run_live(root, sorted(set(ids)), args.sha, wait_min=args.wait_min)
    report.print()
    return report.code()


if __name__ == "__main__":
    raise SystemExit(main())
