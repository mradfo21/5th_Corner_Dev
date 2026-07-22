"""
test_cast_store.py — offline unit tests for cast_store.py (load/save/reset
for cast.json's story-bible character roster). Uses temp files so it never
mutates the real committed cast.json / cast.defaults.json.

Run with:
    python3 -m unittest test_cast_store -v
"""

import json
import tempfile
import unittest
from pathlib import Path

import cast_store as cst


SAMPLE_CAST = [
    {"name": "Alice", "role": "Scout", "affiliation": "Independent",
     "description": "Finds the way.", "important": True, "notes": ""},
    {"name": "Bob", "role": "Medic", "affiliation": "Relief Corps",
     "description": "Patches wounds.", "important": False, "notes": "Quiet."},
]


class CastStoreTestCase(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        tmp_path = Path(self._tmpdir.name)

        self._orig_cast_path = cst.CAST_PATH
        self._orig_defaults_path = cst.DEFAULTS_PATH
        cst.CAST_PATH = tmp_path / "cast.json"
        cst.DEFAULTS_PATH = tmp_path / "cast.defaults.json"

        with cst.CAST_PATH.open("w", encoding="utf-8") as f:
            json.dump(SAMPLE_CAST, f)
        with cst.DEFAULTS_PATH.open("w", encoding="utf-8") as f:
            json.dump(SAMPLE_CAST, f)

        cst._cached_cast = []
        cst._cache_mtime = 0.0

    def tearDown(self):
        cst.CAST_PATH = self._orig_cast_path
        cst.DEFAULTS_PATH = self._orig_defaults_path
        cst._cached_cast = []
        cst._cache_mtime = 0.0
        self._tmpdir.cleanup()

    def test_load_cast(self):
        cast = cst.load_cast(force=True)
        self.assertEqual(len(cast), 2)
        self.assertEqual(cast[0]["name"], "Alice")

    def test_save_cast_persists_and_reloads(self):
        new_cast = SAMPLE_CAST + [{"name": "Carol", "role": "Driver"}]
        cst.save_cast(new_cast)
        self.assertEqual(len(cst.load_cast(force=True)), 3)
        with cst.CAST_PATH.open("r", encoding="utf-8") as f:
            on_disk = json.load(f)
        self.assertEqual(on_disk[-1]["name"], "Carol")

    def test_save_cast_removes_entries(self):
        cst.save_cast([SAMPLE_CAST[0]])
        self.assertEqual(len(cst.load_cast(force=True)), 1)

    def test_save_cast_rejects_non_list(self):
        with self.assertRaises(ValueError):
            cst.save_cast({"not": "a list"})

    def test_save_cast_rejects_missing_name(self):
        with self.assertRaises(ValueError):
            cst.save_cast([{"role": "No name here"}])

    def test_reset_cast_restores_default(self):
        cst.save_cast([{"name": "Someone Else"}])
        cst.reset_cast()
        self.assertEqual(cst.load_cast(force=True), SAMPLE_CAST)

    def test_load_defaults_does_not_mutate_live_cast(self):
        cst.save_cast([{"name": "Only One"}])
        defaults = cst.load_defaults()
        self.assertEqual(len(defaults), 2)
        self.assertEqual(len(cst.load_cast(force=True)), 1)


if __name__ == "__main__":
    unittest.main()
