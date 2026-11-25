import importlib.util
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("build_published", ROOT / "scripts" / "build_published.py")
bp = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bp)


def commit(root: Path, day: str, message: str) -> None:
    env = dict(os.environ, GIT_AUTHOR_DATE=f"{day}T12:00:00Z", GIT_COMMITTER_DATE=f"{day}T12:00:00Z")
    for args in (["add", "-A"], ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", message]):
        subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True, env=env)


class BuildPublishedTest(unittest.TestCase):
    def test_days_come_from_first_parent_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "-C", tmp, "init", "-q"], check=True)
            items = root / "news" / "items"
            ru = root / "news" / "translations" / "ru"
            items.mkdir(parents=True)
            ru.mkdir(parents=True)
            (items / "n-aaaaaaaaaa.json").write_text('{"v": 1}')
            commit(root, "2026-07-01", "add item")
            (ru / "n-aaaaaaaaaa.json").write_text('{"t": 1}')
            commit(root, "2026-07-03", "translate")
            (items / "n-aaaaaaaaaa.json").write_text('{"v": 2}')
            commit(root, "2026-07-05", "relations")
            (items / "n-bbbbbbbbbb.json").write_text('{"v": 1}')  # not committed yet
            data = bp.build(root, today="2026-07-09")["items"]
            self.assertEqual(data["n-aaaaaaaaaa"], {"published": "2026-07-01", "modified": "2026-07-05",
                                                    "locales": {"ru": "2026-07-05"}})
            self.assertEqual(data["n-bbbbbbbbbb"]["published"], "2026-07-09")

    def test_shallow_clone_is_not_checked(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp, "src")
            (src / "news" / "items").mkdir(parents=True)
            subprocess.run(["git", "-C", str(src), "init", "-q"], check=True)
            (src / "news" / "items" / "n-aaaaaaaaaa.json").write_text("{}")
            commit(src, "2026-07-01", "a")
            commit_file = src / "news" / "items" / "n-aaaaaaaaaa.json"
            commit_file.write_text('{"x": 1}')
            commit(src, "2026-07-02", "b")
            shallow = Path(tmp, "shallow")
            subprocess.run(["git", "clone", "-q", "--depth", "1", f"file://{src.as_posix()}", str(shallow)],
                           check=True, capture_output=True)
            self.assertEqual(bp.main(["--root", str(shallow)]), 2)
            self.assertFalse((shallow / "news" / "_published.json").exists())


if __name__ == "__main__":
    unittest.main()
