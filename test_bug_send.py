"""A bug report reaches 5th Corner without a key in it, and the intake holds.

Invariants from docs/plans/DISTRIBUTION_MVP_PLAN.md, M5:

- every text file in the package is redacted; the build's version rides along;
- the package never exceeds MAX_BYTES (big images are dropped first);
- the intake refuses oversize bodies, and a sixth report from one address in
  an hour; it stores what it takes and answers a receipt;
- a desktop app is not an intake, and a hosted server does not send.

    python -m unittest test_bug_send -v
"""
from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

import bug_send
import local_guard

KEY = "AIza" + "Q" * 35


class _Bug(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.bugs = Path(self._tmp.name) / "bugs"
        folder = self.bugs / "20260925_120000"
        folder.mkdir(parents=True)
        (folder / "REPORT.md").write_text(f"# Bug\nthe frame went black\nurl ?key={KEY}\n", encoding="utf-8")
        (folder / "state.json").write_text(json.dumps({"k": KEY}), encoding="utf-8")
        (folder / "frame.png").write_bytes(b"\x89PNG" + b"0" * 1000)
        self.id = folder.name
        p = patch.object(bug_send, "_bugs_dir", lambda: self.bugs)
        p.start()
        self.addCleanup(p.stop)


class ThePackage(_Bug):

    def test_text_is_redacted_and_the_version_rides_along(self):
        data, names = bug_send.package(self.id)
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            for n in z.namelist():
                self.assertNotIn(KEY.encode(), z.read(n), n)
            build = json.loads(z.read("build.json"))
        self.assertIn("version", build)
        self.assertIn("frame.png", names)

    def test_it_stays_under_the_cap(self):
        (self.bugs / self.id / "huge.png").write_bytes(os.urandom(bug_send.MAX_BYTES + 10))
        data, names = bug_send.package(self.id)
        self.assertLessEqual(len(data), bug_send.MAX_BYTES)
        self.assertNotIn("huge.png", names)
        self.assertIn("REPORT.md", names)

    def test_the_manifest_says_what_is_redacted(self):
        files = {f["name"]: f for f in bug_send.manifest(self.id)}
        self.assertTrue(files["REPORT.md"]["redacted"])
        self.assertFalse(files["frame.png"]["redacted"])

    def test_a_bad_id_is_nothing(self):
        self.assertIsNone(bug_send.manifest("../../etc"))


class TheIntake(_Bug):

    @classmethod
    def setUpClass(cls):
        import api
        cls.api = api

    def setUp(self):
        super().setUp()
        self.client = self.api.app.test_client()
        bug_send._hits.clear()
        self.root = Path(self._tmp.name) / "site"
        p = patch.object(self.api.engine, "DATA", self.root)
        p.start()
        self.addCleanup(p.stop)
        self.addCleanup(local_guard.disarm)

    def post(self, data: bytes, ip="203.0.113.9"):
        return self.client.post("/api/bug/intake", data={
            "report": (io.BytesIO(data), "r.zip"), "note": f"it broke {KEY}"},
            headers={"X-Forwarded-For": ip, "X-ABYSS-Version": "0.1.0"},
            content_type="multipart/form-data")

    def test_a_report_is_stored_with_a_receipt(self):
        data, _ = bug_send.package(self.id)
        r = self.post(data)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        receipt = r.get_json()["receipt"]
        self.assertTrue((self.root / "bugs" / "intake" / f"{receipt}.zip").is_file())

    def test_oversize_is_refused(self):
        r = self.post(b"0" * (bug_send.MAX_BYTES + 1))
        self.assertEqual(r.status_code, 413)

    def test_one_address_is_rate_limited(self):
        data, _ = bug_send.package(self.id)
        codes = [self.post(data, ip="198.51.100.7").status_code for _ in range(bug_send.RATE_PER_HOUR + 1)]
        self.assertEqual(codes[-1], 429)
        self.assertEqual(self.post(data, ip="198.51.100.8").status_code, 200)

    def test_a_desktop_app_is_not_an_intake(self):
        local_guard.arm("t" * 43, 5000)
        r = self.client.post("/api/bug/intake", base_url="http://127.0.0.1:5000",
                             headers={local_guard.HEADER: "t" * 43})
        self.assertEqual(r.status_code, 404)

    def test_a_hosted_server_does_not_send(self):
        with patch.object(self.api, "_shutdown_armed", False):
            r = self.client.post("/api/bug/send", json={"id": self.id})
        self.assertEqual(r.status_code, 403)


if __name__ == "__main__":
    unittest.main()
