import copy
import importlib.util
import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "build_news", ROOT / "scripts" / "build_news.py"
)
assert SPEC is not None and SPEC.loader is not None
build_news = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(build_news)


class NewsContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.base_item = json.loads(
            (ROOT / "news" / "items" / "n-0124eabefd.json").read_text(encoding="utf-8")
        )

    def item(self, item_id, posted_at, family_display):
        item = copy.deepcopy(self.base_item)
        item["id"] = item_id
        item["source"]["posted_at"] = posted_at
        item["facets"]["year"] = int(posted_at[:4])
        item["project"]["family_slug"] = "aggregation-test"
        item["project"]["family_display"] = family_display
        return item

    def v14_item(
        self,
        item_id,
        posted_at,
        *,
        scope="project",
        thread_slug="news-knowledge-graph",
        predecessors=None,
    ):
        item = self.item(item_id, posted_at, "Aggregation Test")
        item["schema_version"] = "1.4"
        item["project"]["vendor"] = "Happyin"
        item["project"]["vendor_slug"] = "happyin"
        item["organization"] = {
            "slug": "happyin",
            "display": "Happyin",
            "homepage": "https://happyin.app/",
        }
        item["development"] = {
            "scope": scope,
            "thread_slug": thread_slug,
            "thread_display": "News and knowledge graph",
            "predecessor_ids": list(predecessors or []),
        }
        item["lifecycle_event"] = {
            "kind": "milestone",
            "date": posted_at,
            "title": item["title"],
            "version": None,
            "summary": item["summary"],
        }
        item["knowledge"] = {
            "title": "News Development Graph",
            "article_url": "https://happyin.space/architecture/news-development-graph/",
            "source_url": (
                "https://raw.githubusercontent.com/AnastasiyaW/knowledge-space/"
                "0123456789abcdef0123456789abcdef01234567/"
                "docs/architecture/news-development-graph.md"
            ),
            "agent_context_url": (
                "https://happyin.space/architecture/news-development-graph/#agent-brief"
            ),
        }
        return item

    def link(self, kind, is_primary, verified_at):
        return {
            "kind": kind,
            "url": "https://example.test/project",
            "host": "example.test",
            "lang": "en",
            "is_primary": is_primary,
            "status": "live",
            "verified_at": verified_at,
        }

    def test_project_uses_latest_display_and_preserves_history(self):
        older = self.item("n-1111111111", "2026-01-01", "Older Name")
        newer = self.item("n-2222222222", "2026-01-02", "Current Name")

        project = build_news.aggregate_project("aggregation-test", [newer, older])

        self.assertEqual(project["family_display"], "Current Name")
        self.assertEqual(
            project["family_display_variants"],
            [
                {
                    "family_display": "Older Name",
                    "first_seen": "2026-01-01",
                    "last_seen": "2026-01-01",
                    "news_ids": ["n-1111111111"],
                },
                {
                    "family_display": "Current Name",
                    "first_seen": "2026-01-02",
                    "last_seen": "2026-01-02",
                    "news_ids": ["n-2222222222"],
                },
            ],
        )

    def test_project_rejects_incompatible_display_on_any_same_date(self):
        left = self.item("n-3333333333", "2026-01-03", "One Name")
        right = self.item("n-4444444444", "2026-01-03", "Other Name")
        newest = self.item("n-5555555555", "2026-01-04", "Current Name")

        with self.assertRaisesRegex(build_news.ContractError, "conflicting family displays on date"):
            build_news.aggregate_project("aggregation-test", [newest, left, right])

    def test_link_rejects_incompatible_shared_metadata(self):
        left = self.item("n-4444444444", "2026-01-01", "Link Test")
        right = self.item("n-5555555555", "2026-01-02", "Link Test")
        left["links"] = [self.link("code", False, None)]
        right["links"] = [self.link("paper", True, "2026-01-02T00:00:00Z")]

        with self.assertRaisesRegex(build_news.ContractError, "conflicting non-null 'kind'"):
            build_news.aggregate_links([left, right])

    def test_link_preserves_contextual_primary_and_verification_metadata(self):
        older = self.item("n-6666666666", "2026-01-01", "Link Test")
        newer = self.item("n-7777777777", "2026-01-02", "Link Test")
        older["links"] = [self.link("other", False, None)]
        newer["links"] = [self.link("other", True, "2026-01-02T00:00:00Z")]

        link = build_news.aggregate_links([newer, older])[0]

        self.assertTrue(link["is_primary"])
        self.assertEqual(link["verified_at"], "2026-01-02T00:00:00Z")
        self.assertEqual(link["news_ids"], ["n-6666666666", "n-7777777777"])
        self.assertEqual(
            link["occurrences"],
            [
                {"news_id": "n-6666666666", "is_primary": False, "verified_at": None},
                {
                    "news_id": "n-7777777777",
                    "is_primary": True,
                    "verified_at": "2026-01-02T00:00:00Z",
                },
            ],
        )

    def test_schema_validated_must_be_literal_true(self):
        item = copy.deepcopy(self.base_item)
        item["_meta"]["schema_validated"] = False

        self.assertIn(
            "_meta.schema_validated: expected literal true",
            build_news.validate_item(item, item["id"] + ".json"),
        )

    def test_v14_item_validates_with_explicit_graph_and_knowledge_bridge(self):
        item = self.v14_item("n-8888888888", "2026-01-02")

        self.assertEqual([], build_news.validate_item(item, item["id"] + ".json"))

    def test_v14_rejects_mutable_knowledge_source_and_prompt_field(self):
        item = self.v14_item("n-8888888888", "2026-01-02")
        item["knowledge"]["source_url"] = (
            "https://raw.githubusercontent.com/AnastasiyaW/knowledge-space/"
            "master/docs/architecture/news-development-graph.md"
        )
        item["knowledge"]["prompt"] = "private-looking prompt"

        errors = build_news.validate_item(item, item["id"] + ".json")

        self.assertTrue(any("commit-addressed" in error for error in errors))
        self.assertIn("$.knowledge.prompt: private/raw field is forbidden in the public feed", errors)

    def test_development_graph_rejects_missing_cross_thread_and_forward_edges(self):
        older = self.v14_item("n-1111111111", "2026-01-01")
        cross_thread = self.v14_item(
            "n-2222222222",
            "2026-01-02",
            thread_slug="other-thread",
            predecessors=[older["id"]],
        )
        missing = self.v14_item(
            "n-3333333333",
            "2026-01-03",
            predecessors=["n-9999999999"],
        )
        forward = self.v14_item(
            "n-4444444444",
            "2026-01-04",
            predecessors=["n-5555555555"],
        )
        later = self.v14_item("n-5555555555", "2026-01-05")

        errors = build_news.validate_development_graph(
            [older, cross_thread, missing, forward, later]
        )

        self.assertTrue(any("different development thread" in error for error in errors))
        self.assertTrue(any("references missing" in error for error in errors))
        self.assertTrue(any("must be strictly earlier" in error for error in errors))

    def test_project_aggregates_explicit_development_threads(self):
        older = self.v14_item("n-1111111111", "2026-01-01")
        newer = self.v14_item(
            "n-2222222222", "2026-01-02", predecessors=[older["id"]]
        )

        project = build_news.aggregate_project("aggregation-test", [newer, older])

        self.assertEqual(project["organization"]["slug"], "happyin")
        self.assertEqual(
            project["development_threads"],
            [
                {
                    "slug": "news-knowledge-graph",
                    "display": "News and knowledge graph",
                    "first_seen": "2026-01-01",
                    "last_seen": "2026-01-02",
                    "latest_news_id": "n-2222222222",
                    "news_ids": ["n-1111111111", "n-2222222222"],
                }
            ],
        )

    def test_organization_joins_direct_news_and_owned_projects(self):
        project_item = self.v14_item("n-1111111111", "2026-01-01")
        company_item = self.v14_item(
            "n-2222222222", "2026-01-02", scope="organization"
        )
        company_item["project"] = {
            key: None for key in build_news.PROJECT_REQUIRED_FIELDS
        }
        company_item["facets"]["projects"] = []

        organizations = build_news.aggregate_organizations([company_item, project_item])
        company = organizations["happyin"]

        self.assertEqual(company["project_slugs"], ["aggregation-test"])
        self.assertEqual(company["direct_news_ids"], ["n-2222222222"])
        self.assertEqual(company["news_ids"], ["n-1111111111", "n-2222222222"])
        self.assertEqual(company["development_threads"][0]["slug"], "news-knowledge-graph")



class SourcePermalinkTest(unittest.TestCase):
    """The pipeline writes source.permalink since 2026-11-22; the builder admits exactly that."""

    def errors(self, source):
        errors = []
        build_news.validate_source(errors, {"source": source})
        return errors

    def test_anchor_post_permalink_is_admitted(self):
        self.assertEqual([], self.errors({"posted_at": "2026-11-26", "excerpt": "x",
                                          "permalink": "https://t.me/GreenNeuralRobots/14550"}))
        self.assertEqual([], self.errors({"posted_at": "2026-11-26", "excerpt": "x"}))

    def test_other_permalinks_and_fields_are_refused(self):
        for bad in ("https://t.me/other/1", "http://t.me/GreenNeuralRobots/1", 14550,
                    "https://t.me/GreenNeuralRobots/1?x=1"):
            with self.subTest(permalink=bad):
                self.assertIn("source.permalink: expected https://t.me/GreenNeuralRobots/<post id>",
                              self.errors({"posted_at": "2026-11-26", "excerpt": "x", "permalink": bad}))
        self.assertIn("source.url: unexpected field",
                      self.errors({"posted_at": "2026-11-26", "excerpt": "x", "url": "https://example.com"}))

if __name__ == "__main__":
    unittest.main()
