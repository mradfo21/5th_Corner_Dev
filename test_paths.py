"""A packaged build keeps the player's things apart from the program.

Invariants from docs/plans/DISTRIBUTION_MVP_PLAN.md, M2:

- run from source, the data root IS the repo (development unchanged);
- the one-time move from %APPDATA%\\SOMEWHERE copies, never deletes, and runs
  once: a second launch does not copy again over what the player has since
  changed;
- an update refreshes a factory file only while the player's copy is still the
  factory copy it was seeded from; a file the player changed is theirs;
- prepare() points the stores' own overrides at the data root and creates the
  folders the game writes into.

    python -m unittest test_paths -v
"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import app_identity
import paths


class _Tmp(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        env = patch.dict(os.environ, {})
        env.start()
        self.addCleanup(env.stop)
        for k in list(paths.store_env(Path(".")).keys()) + [paths.ENV]:
            os.environ.pop(k, None)


class FromSourceNothingMoves(_Tmp):

    def test_the_data_root_is_the_repo(self):
        with patch.object(paths, "FROZEN", False):
            self.assertEqual(paths.data_root(), paths.install_root())

    def test_prepare_from_source_sets_no_overrides(self):
        with patch.object(paths, "FROZEN", False):
            paths.prepare()
        self.assertNotIn("SESSIONS_DIR", os.environ)

    def test_the_override_wins(self):
        os.environ[paths.ENV] = str(self.tmp / "d")
        self.assertEqual(paths.data_root(), self.tmp / "d")


class TheOldFolderMovesOnce(_Tmp):

    def setUp(self):
        super().setUp()
        os.environ["APPDATA"] = str(self.tmp)
        old = self.tmp / app_identity.LEGACY_DATA_DIR_NAME
        (old / "characters" / "c1").mkdir(parents=True)
        (old / "keys.env").write_text("GEMINI_API_KEY=x\n", encoding="utf-8")
        (old / "characters" / "c1" / "character.json").write_text("{}", encoding="utf-8")
        self.old = old

    def test_it_is_copied_and_the_old_folder_stays(self):
        new = app_identity.data_dir()
        self.assertEqual(new, self.tmp / app_identity.DATA_DIR_NAME)
        self.assertEqual((new / "keys.env").read_text(encoding="utf-8"), "GEMINI_API_KEY=x\n")
        self.assertTrue((new / "characters" / "c1" / "character.json").is_file())
        self.assertTrue((self.old / "keys.env").is_file())

    def test_it_runs_once(self):
        new = app_identity.data_dir()
        (new / "keys.env").write_text("GEMINI_API_KEY=changed\n", encoding="utf-8")
        app_identity.data_dir()
        self.assertEqual((new / "keys.env").read_text(encoding="utf-8"), "GEMINI_API_KEY=changed\n")

    def test_no_old_folder_is_fine(self):
        os.environ["APPDATA"] = str(self.tmp / "fresh")
        new = app_identity.data_dir()
        self.assertFalse(new.exists())   # created by prepare(), not here


class UpdatesRespectThePlayersFiles(_Tmp):

    def setUp(self):
        super().setUp()
        self.src = self.tmp / "install"
        self.dst = self.tmp / "data"
        self.write(self.src, "prompts/simulation_prompts.json", "factory v1")
        self.write(self.src, "worlds/somewhere.json", "world v1")
        self.write(self.src, "experiences/somewhere.json", "exp v1")
        self.write(self.src, "experiences/.active", "machine pointer")

    def write(self, root, rel, text):
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")

    def read(self, rel):
        return (self.dst / rel).read_text(encoding="utf-8")

    def test_first_run_seeds_and_skips_the_machine_pointer(self):
        done = paths.seed_factory(self.src, self.dst)
        self.assertEqual(done["prompts/simulation_prompts.json"], "seeded")
        self.assertEqual(self.read("worlds/somewhere.json"), "world v1")
        self.assertFalse((self.dst / "experiences" / ".active").exists())

    def test_an_untouched_file_takes_the_update(self):
        paths.seed_factory(self.src, self.dst)
        self.write(self.src, "worlds/somewhere.json", "world v2")
        done = paths.seed_factory(self.src, self.dst)
        self.assertEqual(done.get("worlds/somewhere.json"), "refreshed")
        self.assertEqual(self.read("worlds/somewhere.json"), "world v2")

    def test_a_file_the_player_changed_is_theirs(self):
        paths.seed_factory(self.src, self.dst)
        self.write(self.dst, "prompts/simulation_prompts.json", "bound to my World")
        self.write(self.src, "prompts/simulation_prompts.json", "factory v2")
        done = paths.seed_factory(self.src, self.dst)
        self.assertNotIn("prompts/simulation_prompts.json", done)
        self.assertEqual(self.read("prompts/simulation_prompts.json"), "bound to my World")

    def test_the_players_own_worlds_are_never_touched(self):
        paths.seed_factory(self.src, self.dst)
        self.write(self.dst, "worlds/my-level.json", "mine")
        paths.seed_factory(self.src, self.dst)
        self.assertEqual(self.read("worlds/my-level.json"), "mine")

    def test_nothing_is_done_when_the_roots_are_the_same(self):
        self.assertEqual(paths.seed_factory(self.src, self.src), {})


class PrepareFrozen(_Tmp):

    def test_it_points_the_stores_at_the_data_root(self):
        os.environ[paths.ENV] = str(self.tmp / "data")
        root = paths.prepare()
        self.assertEqual(root, self.tmp / "data")
        self.assertEqual(os.environ["SESSIONS_DIR"], str(root / "sessions"))
        self.assertEqual(os.environ["SOMEWHERE_PROMPTS_PATH"],
                         str(root / "prompts" / "simulation_prompts.json"))
        for rel in ("sessions", "logs", "archives", "worlds", "characters"):
            self.assertTrue((root / rel).is_dir(), rel)
        self.assertTrue((root / "prompts" / "simulation_prompts.json").is_file())


if __name__ == "__main__":
    unittest.main()
