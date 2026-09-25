"""The desktop app's server answers its own window and nothing else.

Invariants from docs/plans/DISTRIBUTION_MVP_PLAN.md, M1 — each one a way a web
page open in any browser on the player's machine could reach the running game
before 2026-09-25:

- armed, a request for the run's state without the launch cookie or header is
  refused, and so is one addressed to any other Host (DNS rebinding);
- the launch URL trades its token for an HttpOnly, SameSite=Strict cookie;
- the desktop app answers no one cross-origin; a hosted server answers only
  the site's own origins;
- unarmed (hosted, run_local.py, every other suite) nothing changes;
- an archive name cannot leave archives/ — `..\\worlds` was an rmtree of the
  authoring folder, on hosted servers too;
- a blank custom key never carries the stored key to a new address.

    python -m unittest test_local_guard -v
"""
from __future__ import annotations

import os
import secrets
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import local_guard
import safe_log

PORT = 51234
BASE = f"http://127.0.0.1:{PORT}"


class _Case(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import api
        cls.api = api

    def setUp(self):
        self.token = secrets.token_urlsafe(32)
        self.client = self.api.app.test_client()
        self.addCleanup(local_guard.disarm)
        env = patch.dict(os.environ, {})
        env.start()
        self.addCleanup(env.stop)

    def arm(self):
        local_guard.arm(self.token, PORT)


class ArmedRefusesStrangers(_Case):

    def test_no_token_is_refused_on_every_guarded_prefix(self):
        self.arm()
        for path in ("/api/health", "/api/keys", "/api/state", "/images/x.png",
                     "/audio/x.mp3", "/ws/talk/live"):
            r = self.client.get(path, base_url=BASE)
            self.assertEqual(r.status_code, 403, path)

    def test_state_changing_calls_without_a_token_are_refused(self):
        self.arm()
        r = self.client.put("/api/keys/custom", base_url=BASE,
                            json={"address": "https://evil.example/v1", "model": "m", "value": ""})
        self.assertEqual(r.status_code, 403)
        r = self.client.post("/api/shutdown", base_url=BASE)
        self.assertEqual(r.status_code, 403)

    def test_the_header_lets_a_python_client_in(self):
        self.arm()
        r = self.client.get("/api/health", base_url=BASE,
                            headers={local_guard.HEADER: self.token})
        self.assertEqual(r.status_code, 200)

    def test_a_wrong_token_is_refused(self):
        self.arm()
        r = self.client.get("/api/health", base_url=BASE,
                            headers={local_guard.HEADER: "x" * 43})
        self.assertEqual(r.status_code, 403)

    def test_another_host_is_refused_even_with_the_token(self):
        """DNS rebinding: evil.example re-pointed at 127.0.0.1 still says so."""
        self.arm()
        for base in (f"http://evil.example:{PORT}", "http://127.0.0.1:9", f"http://localhost.evil:{PORT}"):
            r = self.client.get("/api/health", base_url=base,
                                headers={local_guard.HEADER: self.token})
            self.assertEqual(r.status_code, 403, base)
        r = self.client.get("/api/health", base_url=f"http://localhost:{PORT}",
                            headers={local_guard.HEADER: self.token})
        self.assertEqual(r.status_code, 200)

    def test_the_launch_url_hands_the_page_a_strict_httponly_cookie(self):
        self.arm()
        r = self.client.get(f"/standalone?fresh=1&launch={self.token}", base_url=BASE)
        self.assertEqual(r.status_code, 200)
        cookie = r.headers.get("Set-Cookie") or ""
        self.assertIn(f"{local_guard.COOKIE}=", cookie)
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Strict", cookie)
        # ...and the page's own requests carry it from then on.
        r = self.client.get("/api/health", base_url=BASE)
        self.assertEqual(r.status_code, 200)

    def test_a_wrong_launch_param_sets_no_cookie(self):
        self.arm()
        r = self.client.get("/standalone?launch=nope", base_url=BASE)
        self.assertNotIn(local_guard.COOKIE, r.headers.get("Set-Cookie") or "")

    def test_the_page_itself_loads_without_a_token(self):
        """The shell and its static files are the game's code, not the run."""
        self.arm()
        self.assertEqual(self.client.get("/standalone", base_url=BASE).status_code, 200)

    def test_the_desktop_app_answers_no_origin_cross_site(self):
        self.arm()
        for origin in ("https://evil.example", "https://www.5th-corner.com"):
            r = self.client.get("/api/health", base_url=BASE, headers={
                "Origin": origin, local_guard.HEADER: self.token})
            self.assertNotIn("Access-Control-Allow-Origin", r.headers, origin)

    def test_arming_hands_the_token_to_children(self):
        self.arm()
        self.assertEqual(os.environ.get(local_guard.ENV), self.token)
        self.assertEqual(local_guard.client_headers(), {local_guard.HEADER: self.token})


class UnarmedIsUnchanged(_Case):

    def test_hosted_and_run_local_need_no_token(self):
        self.assertEqual(self.client.get("/api/health").status_code, 200)

    def test_hosted_cors_is_the_sites_origins_only(self):
        r = self.client.get("/api/health", headers={"Origin": "https://evil.example"})
        self.assertNotIn("Access-Control-Allow-Origin", r.headers)
        r = self.client.get("/api/health", headers={"Origin": "https://www.5th-corner.com"})
        self.assertEqual(r.headers.get("Access-Control-Allow-Origin"), "https://www.5th-corner.com")


class TheAgeQuestion(_Case):
    """A packaged build asks once (age_gate.js); from source it never does."""

    def setUp(self):
        super().setUp()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        p = patch.object(self.api.engine, "DATA", Path(self._tmp.name))
        p.start()
        self.addCleanup(p.stop)

    def test_from_source_it_is_not_asked(self):
        os.environ.pop("ABYSS_AGE_GATE", None)
        with patch("paths.FROZEN", False):
            self.assertFalse(self.client.get("/api/consent").get_json()["required"])

    def test_a_build_asks_once_and_remembers(self):
        with patch("paths.FROZEN", True):
            c = self.client.get("/api/consent").get_json()
            self.assertEqual((c["required"], c["confirmed"]), (True, False))
            self.assertEqual(self.client.post("/api/consent", json={"adult": False}).status_code, 400)
            self.assertEqual(self.client.post("/api/consent", json={"adult": True}).status_code, 200)
            self.assertTrue(self.client.get("/api/consent").get_json()["confirmed"])
        self.assertTrue((Path(self._tmp.name) / "consent.json").is_file())


class ArchivesStayInArchives(_Case):
    """Relative to the working directory, like the routes themselves."""

    def setUp(self):
        super().setUp()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._cwd = os.getcwd()
        os.chdir(self._tmp.name)
        self.addCleanup(os.chdir, self._cwd)
        Path("archives/run_1/images").mkdir(parents=True)
        Path("victim").mkdir()
        Path("victim/world.json").write_text("{}", encoding="utf-8")

    def test_backslash_traversal_cannot_delete_a_sibling(self):
        for name in ("..%5Cvictim", "..%5C..%5Cvictim", "..", "%2E%2E"):
            r = self.client.delete(f"/api/archives/{name}")
            self.assertIn(r.status_code, (400, 404), name)
        self.assertTrue(Path("victim/world.json").exists())

    def test_backslash_traversal_cannot_read_a_sibling(self):
        r = self.client.get("/api/archives/..%5Cvictim")
        self.assertEqual(r.status_code, 400)
        r = self.client.get("/api/archives/..%5Cvictim/images/world.json")
        self.assertEqual(r.status_code, 400)

    def test_a_real_archive_is_still_served_and_deleted(self):
        self.assertEqual(self.client.get("/api/archives/run_1").status_code, 200)
        self.assertEqual(self.client.delete("/api/archives/run_1").status_code, 200)
        self.assertFalse(Path("archives/run_1").exists())


class CustomKeyStaysWithItsAddress(unittest.TestCase):

    def setUp(self):
        import keys_store
        self.ks = keys_store
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "keys.env"
        for name in ("_set_narrator", "_rebind_openai_client", "refresh_runtime_keys"):
            p = patch.object(keys_store, name, lambda *a, **k: None)
            p.start()
            self.addCleanup(p.stop)
        env = patch.dict(os.environ, {})
        env.start()
        self.addCleanup(env.stop)

    def stored(self):
        return self.ks._read_env_file(self.path)

    def test_a_new_address_does_not_inherit_the_stored_key(self):
        real = "sk-proj-" + "A" * 40
        self.ks._atomic_write_env(self.path, {"OPENAI_API_KEY": real})
        self.ks.set_custom("https://evil.example/v1", "m", "", path=self.path)
        self.assertNotEqual(self.stored().get("OPENAI_API_KEY"), real)
        self.assertNotEqual(os.environ.get("OPENAI_API_KEY"), real)

    def test_the_same_address_keeps_its_key_when_the_model_changes(self):
        mine = "sk-local-" + "B" * 30
        self.ks.set_custom("http://localhost:11434/v1", "llama3.1", mine, path=self.path)
        self.ks.set_custom("http://localhost:11434/v1/", "qwen2.5", "", path=self.path)
        self.assertEqual(self.stored().get("OPENAI_API_KEY"), mine)


class LogsHoldNoKeys(unittest.TestCase):

    def test_key_shapes_are_redacted(self):
        samples = [
            "AIza" + "S" * 35,
            "sk-proj-" + "a1" * 20,
            "sk-ant-api03-" + "x" * 40,
            "sk_live_" + "9" * 24,
            "whsec_" + "w" * 32,
            "sk_" + "0f" * 24,
            "r8_" + "R" * 37,
        ]
        for s in samples:
            out = safe_log.redact(f"POST https://x/y?key={s} failed: {s}")
            self.assertNotIn(s, out, s)

    def test_the_launch_token_is_redacted(self):
        t = secrets.token_urlsafe(32)
        safe_log.add_secret(t)
        self.assertNotIn(t, safe_log.redact(f"loading /standalone?launch={t}"))

    def test_ordinary_lines_survive(self):
        line = "[play] boot 1727265600  (this process is the live build) turn 12 ok"
        self.assertEqual(safe_log.redact(line), line)

    def test_the_log_rotates(self):
        with tempfile.TemporaryDirectory() as d:
            log = Path(d) / "somewhere.log"
            s = safe_log.RotatingRedactingStream(log, max_bytes=1000, backups=3)
            for i in range(60):
                s.write(f"line {i:04d} " + "." * 80 + "\n")
            s.flush()
            s._fh.close()
            self.assertTrue(log.with_name("somewhere.log.1").exists())
            self.assertFalse(log.with_name("somewhere.log.4").exists())
            self.assertLessEqual(log.stat().st_size, 1000)


if __name__ == "__main__":
    unittest.main()
