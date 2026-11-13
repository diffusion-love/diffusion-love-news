import importlib.util
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("translate_items", ROOT / "scripts" / "translate_items.py")
ti = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ti)

SOURCE = {
    "title": "FLUX.2 [klein] 9B ships a LoRA trainer",
    "summary": "Black Forest Labs released FLUX.2 klein 9B with `diffusers` support; weights at https://huggingface.co/black-forest-labs/FLUX.2-klein-9B today.",
}
GOOD_DE = {
    "title": "FLUX.2 [klein] 9B erhält einen LoRA-Trainer",
    "summary": "Black Forest Labs hat FLUX.2 klein 9B mit `diffusers`-Unterstützung veröffentlicht; Gewichte unter https://huggingface.co/black-forest-labs/FLUX.2-klein-9B heute.",
}


class CheckTranslation(unittest.TestCase):
    def test_a_faithful_translation_passes(self):
        self.assertEqual(ti.check_translation("de", SOURCE, GOOD_DE), GOOD_DE)

    def test_each_broken_rule_is_refused(self):
        cases = {
            "keys": {"title": GOOD_DE["title"]},
            "empty": {**GOOD_DE, "title": " "},
            "url": {**GOOD_DE, "summary": GOOD_DE["summary"].replace("FLUX.2-klein-9B heute", "FLUX2 heute")},
            "code": {**GOOD_DE, "summary": GOOD_DE["summary"].replace("`diffusers`", "diffusers")},
            "name": {**GOOD_DE, "title": "FLUX 2 [klein] 9B erhält einen LoRA-Trainer"},
            "untranslated": dict(SOURCE),
        }
        for name, answer in cases.items():
            with self.subTest(name), self.assertRaises(ValueError):
                ti.check_translation("de", SOURCE, answer)

    def test_words_hyphenated_to_a_name_may_be_translated(self):
        self.assertEqual(ti.name_tokens("K3-related, 1M-token FP8-labelled 1.5-second-hop Qwen2.5-VL-7B "
                                        "mid-2026 10x 1.5x 10-20x 4th 21st 11th 2-Step kimi-k3.1-pro"),
                         ["K3", "1M", "FP8", "Qwen2.5-VL-7B", "k3.1"])
        source = {"title": "Kimi K3-related notes on the 1M-token context window"}
        ok = {"title": "Заметки о Kimi K3 и контекстном окне на 1M токенов"}
        self.assertEqual(ti.check_translation("ru", source, ok), ok)
        with self.assertRaises(ValueError):
            ti.check_translation("ru", source, {"title": "Заметки о Kimi К3 и контекстном окне на 1M токенов"})

    def test_script_languages_must_be_in_their_script(self):
        with self.assertRaises(ValueError):
            ti.check_translation("ru", SOURCE, GOOD_DE)

    def test_hash_ignores_fields_that_are_not_translated(self):
        item = {"title": "A", "summary": "B", "research_notes": "", "evolution": {"summary": None}, "links": [1]}
        other = {**item, "links": [2]}
        self.assertEqual(ti.source_hash(ti.source_fields(item)), ti.source_hash(ti.source_fields(other)))
        self.assertNotEqual(ti.source_hash(ti.source_fields(item)),
                            ti.source_hash(ti.source_fields({**item, "summary": "C"})))

    def test_answer_in_a_code_fence_is_read(self):
        self.assertEqual(ti.parse_answer('```json\n{"title": "x"}\n```'), {"title": "x"})


class JobsAndIngest(unittest.TestCase):
    """File-based jobs go through the same checks as Batches API results."""

    def setUp(self):
        import json
        import tempfile
        self.tmp = Path(tempfile.mkdtemp())
        self.saved = (ti.ITEMS, ti.OUT, ti.STATE)
        ti.ITEMS, ti.OUT, ti.STATE = self.tmp / "items", self.tmp / "translations", self.tmp / "state"
        ti.ITEMS.mkdir()
        for item_id in ("n-0000000001", "n-0000000002"):
            (ti.ITEMS / f"{item_id}.json").write_text(json.dumps({"id": item_id, **SOURCE}), encoding="utf-8")

    def tearDown(self):
        import shutil
        ti.ITEMS, ti.OUT, ti.STATE = self.saved
        shutil.rmtree(self.tmp)

    def test_good_answers_are_stored_and_bad_ones_rejected(self):
        import argparse
        jobs = self.tmp / "jobs"
        ti.cmd_jobs(argparse.Namespace(lang=["de"], out=str(jobs), size=10, only=None))
        job = ti.load_json(jobs / "de-000.json")
        self.assertEqual(set(job["items"]), {"n-0000000001", "n-0000000002"})
        ti.write_json(jobs / "de-000.answer.json", {"n-0000000001": GOOD_DE, "n-0000000002": dict(SOURCE)})
        self.assertEqual(ti.cmd_ingest(argparse.Namespace(folder=str(jobs), model="m")), 1)
        self.assertEqual(set(ti.records("de")), {"n-0000000001"})
        self.assertEqual(ti.records("de")["n-0000000001"]["model"], "m")
        self.assertTrue((ti.STATE / "rejected" / "de" / "n-0000000002.txt").is_file())
        self.assertEqual(set(ti.load_json(ti.OUT / "de.json")), {"n-0000000001"})


class StoredTranslations(unittest.TestCase):
    def test_committed_translations_are_valid_and_bundles_current(self):
        if not ti.OUT.is_dir():
            self.skipTest("no translations yet")
        self.assertEqual(ti.cmd_check(None), 0)


if __name__ == "__main__":
    unittest.main()
