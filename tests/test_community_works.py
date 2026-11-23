import copy
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("build_news", ROOT / "scripts" / "build_news.py")
assert SPEC is not None and SPEC.loader is not None
build_news = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(build_news)

WORK = json.loads((ROOT / "news" / "community" / "cw-agsoft-pi-h3-ref-workflow-v3.json").read_text(encoding="utf-8"))


def with_link(url, kind="workflow"):
    work = copy.deepcopy(WORK)
    work["links"][0] = {"kind": kind, "url": url, "is_primary": True, "label": None}
    return work


class CommunityWorkTest(unittest.TestCase):
    def test_every_published_record_is_valid(self):
        folder = ROOT / "news" / "community"
        for path in sorted(folder.glob("*.json")):
            work = build_news.load_json_text(path.read_text(encoding="utf-8"))
            self.assertEqual(build_news.validate_community(work, path.name), [], path.name)

    def test_a_public_telegram_post_is_a_source_but_a_private_chat_is_not(self):
        self.assertEqual(build_news.validate_community(with_link("https://t.me/prompt_by_art/2022")), [])
        for url in ("https://t.me/c/123/45", "https://t.me/+AbCdEf", "https://t.me/joinchat/xyz"):
            self.assertTrue(build_news.validate_community(with_link(url)), url)
        self.assertTrue(build_news.validate_community(with_link("https://t.me/prompt_by_art/2022", kind="weights")))

    def test_tracking_http_and_primary_count_are_refused(self):
        self.assertTrue(build_news.validate_community(with_link("https://civitai.com/models/1?utm_source=x")))
        self.assertTrue(build_news.validate_community(with_link("https://www.reddit.com/r/x/comments/1/?share_id=abc")))
        self.assertTrue(build_news.validate_community(with_link("http://civitai.com/models/1")))
        work = copy.deepcopy(WORK)
        for link in work["links"]:
            link["is_primary"] = True
        self.assertIn("$.links: exactly one link is the primary source", build_news.validate_community(work))

    def test_shape_author_tags_and_file_name(self):
        work = copy.deepcopy(WORK)
        work["extra"] = 1
        work["author"] = {"name": ""}
        work["tags"]["works_with"] = []
        errors = build_news.validate_community(work, "cw-other.json")
        for expected in ("$.extra: unexpected field", "$.author.name: expected the author's public name",
                         "$.tags.works_with: at least one entry is required", "$.id: file must be named"):
            self.assertTrue(any(error.startswith(expected) for error in errors), (expected, errors))
        self.assertTrue(build_news.validate_community({**WORK, "published_at": "2026-11-22"}))

    def test_the_index_lists_works_newest_first_and_meta_counts_them(self):
        with tempfile.TemporaryDirectory() as tmp:
            news = Path(tmp)
            (news / "community").mkdir()
            older = {**copy.deepcopy(WORK), "id": "cw-older", "published_at": "2026-01-01T00:00:00Z"}
            for work in (WORK, older):
                (news / "community" / f"{work['id']}.json").write_text(json.dumps(work), encoding="utf-8")
            works = build_news.load_community(news)
            self.assertEqual([work["id"] for work in works], [WORK["id"], "cw-older"])
            (news / "community" / "cw-broken.json").write_text(json.dumps({**WORK, "id": "cw-broken", "schema": "x"}), encoding="utf-8")
            with self.assertRaises(build_news.ContractError):
                build_news.load_community(news)
        self.assertEqual(build_news.load_community(Path(tempfile.gettempdir()) / "no-such-news-dir"), [])

    def test_the_committed_index_is_current(self):
        works = build_news.load_community(ROOT / "news")
        records = build_news.load_items(ROOT / "news")
        outputs = build_news.build_outputs(records, works)
        self.assertEqual(outputs["_community.json"], (ROOT / "news" / "_community.json").read_bytes().replace(b"\r\n", b"\n"))
        index = json.loads(outputs["_community.json"])
        meta = json.loads(outputs["_meta.json"])
        self.assertEqual(index["schema"], "diffusion-love/community-index/1")
        self.assertEqual(index["total"], len(works))
        self.assertEqual(meta["counts"]["community"], len(works))
        self.assertGreaterEqual(len(works), 7)


if __name__ == "__main__":
    unittest.main()
