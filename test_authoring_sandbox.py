"""test_authoring_sandbox.py — the guard that stops a test run eating the game.

The defect this exists for, in full: `conftest.py` sandboxed the authoring data
as a pytest fixture, and every test module in this repo documents itself as
`python3 -m unittest <module> -v`. unittest does not load conftest.py. So the
documented way to run the suite was the one way the sandbox could not see, and on
2026-09-17 a `python -m unittest` run of the editor e2e suites wrote
prompts/harness.generic.json over the live prompt file and over worlds/world.json
— taking the Horizon world document and the Level sheet with it — and blanked
tunables.json, which dropped Flipbook to its schema default of off and turned
every animated turn into a still with no error anywhere.

The guard moved into `authoring_sandbox.py` and now engages on IMPORT of any
authoring store, so the runner no longer decides whether the game survives.

These tests are the two halves that can actually be checked:

  * the sandbox is ON right now, in whatever runner you used to get here — if
    this fails, the suite you are running is writing to your real game
  * the assumption underneath it holds: the app imports no test framework, so
    detecting one in sys.modules cannot misfire and put a PLAYER in a sandbox

Run with either, which is the point:
    python3 -m unittest test_authoring_sandbox -v
    python3 -m pytest test_authoring_sandbox -v
"""

import os
import subprocess
import sys
import unittest
from pathlib import Path

import authoring_sandbox

ROOT = Path(__file__).resolve().parent


class TestTheSandboxIsOnRightNow(unittest.TestCase):
    """Whatever runner you used, the writes are contained before this runs."""

    def test_a_framework_was_detected(self):
        self.assertIn(authoring_sandbox.test_framework_in_play(),
                      ("unittest", "pytest", "_pytest"))

    def test_the_sandbox_is_engaged(self):
        self.assertIsNotNone(
            authoring_sandbox.engaged_at(),
            "this test run is writing to the REAL prompts, worlds and tunables")

    def test_every_authoring_path_points_inside_it(self):
        import experience_store
        import prompts_store
        import tunables
        import worlds_store

        sandbox = str(authoring_sandbox.engaged_at())
        for label, path in (
            ("prompts_store.PROMPTS_PATH", prompts_store.PROMPTS_PATH),
            ("prompts_store.DEFAULTS_PATH", prompts_store.DEFAULTS_PATH),
            ("worlds_store.WORLDS_DIR", worlds_store.WORLDS_DIR),
            ("experience_store.EXPERIENCES_DIR", experience_store.EXPERIENCES_DIR),
            ("tunables.STORE", tunables.STORE),
        ):
            self.assertTrue(str(path).startswith(sandbox),
                            f"{label} is {path}, outside the sandbox")

    def test_the_subprocess_channel_is_set_too(self):
        """Playwright suites launch the real app in a child process, which no
        in-process patching reaches. The env vars are how it inherits the
        sandbox, and they are the half that was actually load-bearing."""
        sandbox = str(authoring_sandbox.engaged_at())
        for _mod, _attr, _real, env_var in authoring_sandbox._REDIRECTS:
            self.assertTrue(str(os.environ.get(env_var, "")).startswith(sandbox),
                            f"{env_var} would send a child process to the real files")

    def test_it_is_seeded_with_copies_rather_than_left_empty(self):
        """Contain the writes, not the reads. A test that asserts something about
        shipped content still has to find that content."""
        import prompts_store
        self.assertTrue(Path(prompts_store.PROMPTS_PATH).is_file())
        self.assertIn("narrator_direction",
                      Path(prompts_store.PROMPTS_PATH).read_text(encoding="utf-8"))


def _probe(body: str) -> list:
    """Run `body` in a clean interpreter and return its marked output lines.

    Marked, because importing engine prints ~27 lines of boot log to stdout and
    the answer has to be findable in among them. `errors="replace"` because that
    boot log is written with the console's cp1252 codec on Windows, and one em
    dash in it is otherwise enough to kill the reader thread and hand back
    truncated output — which reads as "the probe printed nothing".

    The environment is stripped of SOMEWHERE_* so the child starts where a real
    app starts. Inheriting them would point it at THIS run's sandbox and quietly
    prove the opposite of what these tests ask.
    """
    env = {k: v for k, v in os.environ.items() if not k.startswith("SOMEWHERE_")}
    src = "import sys\n" + body + "\n"
    out = subprocess.run([sys.executable, "-c", src], cwd=str(ROOT), env=env,
                         capture_output=True, text=True, encoding="utf-8",
                         errors="replace")
    if out.returncode != 0:
        raise AssertionError(f"probe failed:\n{out.stderr[-2000:]}")
    return [line[len("PROBE:"):].strip()
            for line in (out.stdout or "").splitlines()
            if line.startswith("PROBE:")]


class TestTheDetectionCannotMisfire(unittest.TestCase):
    """The guard reads sys.modules for a test framework. If the app ever imports
    one, every PLAYER silently gets a sandbox and their edits stop persisting —
    so the assumption is the thing to pin, not the mechanism."""

    def test_the_app_imports_no_test_framework(self):
        # A clean interpreter, because this test's own runner has already
        # imported one and would answer the question for us.
        found = _probe(
            "import engine\n"
            "print('PROBE:' + ','.join(n for n in ('pytest','_pytest','unittest')"
            " if n in sys.modules))"
        )
        self.assertEqual(
            found, [""],
            "the app now imports a test framework, so authoring_sandbox.guard "
            f"would sandbox a real player: {found}")

    def test_the_real_app_writes_to_the_real_files(self):
        """The other direction: with no framework loaded, nothing is redirected."""
        found = _probe(
            "import authoring_sandbox as s, prompts_store\n"
            "print('PROBE:' + str(s.engaged_at()))\n"
            "print('PROBE:' + str(prompts_store.PROMPTS_PATH))"
        )
        self.assertEqual(found[0], "None", "the app booted into a sandbox")
        self.assertEqual(Path(found[1]),
                         ROOT / "prompts" / "simulation_prompts.json")


class TestImportingAnyStoreIsEnough(unittest.TestCase):
    """The guard sits in four modules because any one of them can be the first
    thing a test imports. Importing exactly one has to arm all of them."""

    def _armed_path_for(self, module: str, attr: str) -> tuple:
        found = _probe(
            f"import unittest\n"
            f"import {module}\n"
            f"import authoring_sandbox as s\n"
            f"print('PROBE:' + str(s.engaged_at() is not None))\n"
            f"print('PROBE:' + str({module}.{attr}))"
        )
        return found[0], found[1]

    def test_prompts_store_alone_arms_it(self):
        armed, path = self._armed_path_for("prompts_store", "PROMPTS_PATH")
        self.assertEqual(armed, "True")
        self.assertNotEqual(Path(path), ROOT / "prompts" / "simulation_prompts.json")

    def test_tunables_alone_arms_it(self):
        armed, path = self._armed_path_for("tunables", "STORE")
        self.assertEqual(armed, "True")
        self.assertNotEqual(Path(path), ROOT / "tunables.json")

    def test_worlds_store_alone_arms_it(self):
        armed, path = self._armed_path_for("worlds_store", "WORLDS_DIR")
        self.assertEqual(armed, "True")
        self.assertNotEqual(Path(path), ROOT / "worlds")

    def test_experience_store_alone_arms_it(self):
        armed, path = self._armed_path_for("experience_store", "EXPERIENCES_DIR")
        self.assertEqual(armed, "True")
        self.assertNotEqual(Path(path), ROOT / "experiences")


if __name__ == "__main__":
    unittest.main(verbosity=2)
