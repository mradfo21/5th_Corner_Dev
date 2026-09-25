"""/get hands a stranger the right file, and says what it is.

Invariants (docs/plans/DISTRIBUTION_MVP_PLAN.md, M5):

- a Velopack release offers its *-Setup.exe, never an update package;
- a release tagged v0.1.0-beta.1 is version 0.1.0-beta.1, not "v0.1.0-beta.1";
- the SHA-256 comes from GitHub's per-asset digest;
- the page knows an installer from a zip (the steps differ);
- the releases repo is the public one, not the source repo.

    python -m unittest test_downloads -v
"""
from __future__ import annotations

import unittest
from unittest.mock import patch

import app_identity
import downloads

SHA = "ab" * 32

VELOPACK_RELEASE = {
    "tag_name": "v0.1.0-beta.1", "name": "ABYSS 0.1.0-beta.1", "draft": False,
    "published_at": "2026-09-25T12:00:00Z", "html_url": "https://github.com/x/y/releases/1",
    "body": "## NEW: It installs\n## FIXED: It updates", "assets": [
        {"id": 1, "name": "5thCorner.ABYSS-0.1.0-beta.1-beta-full.nupkg", "size": 252_000_000},
        {"id": 2, "name": "5thCorner.ABYSS-beta-Portable.zip", "size": 251_000_000},
        {"id": 3, "name": "5thCorner.ABYSS-beta-Setup.exe", "size": 250_000_000,
         "digest": f"sha256:{SHA}", "browser_download_url": "https://example/setup.exe"},
        {"id": 4, "name": "releases.beta.json", "size": 800},
    ]}

ZIP_RELEASE = {
    "tag_name": "build-2026.09.21-c6d8e7f", "name": "ABYSS 2026.09.21", "draft": False,
    "body": "sha256: " + "cd" * 32, "assets": [
        {"id": 9, "name": "ABYSS-2026.09.21-windows.zip", "size": 500_000_000}]}


class WhatGetOffers(unittest.TestCase):

    def test_the_installer_first_never_a_package(self):
        self.assertEqual(downloads._pick_asset(VELOPACK_RELEASE)["name"], "5thCorner.ABYSS-beta-Setup.exe")
        only_packages = {"assets": [a for a in VELOPACK_RELEASE["assets"] if a["name"].endswith(".nupkg")]}
        self.assertIsNone(downloads._pick_asset(only_packages))

    def test_the_portable_zip_when_there_is_no_installer(self):
        rel = {"assets": [a for a in VELOPACK_RELEASE["assets"] if not a["name"].endswith("Setup.exe")]}
        self.assertEqual(downloads._pick_asset(rel)["name"], "5thCorner.ABYSS-beta-Portable.zip")

    def _latest(self, releases):
        with patch.object(downloads, "_get_json", lambda url, headers=None: releases):
            return downloads._from_github()

    def test_version_digest_and_kind(self):
        out = self._latest([VELOPACK_RELEASE])
        self.assertEqual(out["version"], "0.1.0-beta.1")
        self.assertEqual(out["download"]["sha256"], SHA)
        self.assertTrue(out["download"]["installer"])
        self.assertEqual(out["download"]["filename"], "5thCorner.ABYSS-beta-Setup.exe")

    def test_an_old_zip_build_still_works(self):
        out = self._latest([ZIP_RELEASE])
        self.assertEqual(out["version"], "2026.09.21-c6d8e7f")
        self.assertEqual(out["download"]["sha256"], "cd" * 32)
        self.assertFalse(out["download"]["installer"])

    def test_the_releases_repo_is_the_public_one(self):
        with patch.dict("os.environ", {}, clear=False) as env:
            env.pop("GAME_RELEASES_REPO", None)
            self.assertEqual(downloads.releases_repo(), app_identity.RELEASES_REPO)
        self.assertNotEqual(app_identity.RELEASES_REPO, "mradfo21/5th_Corner_Dev")


class TheSiteServesDownloadsOnly(unittest.TestCase):
    """SITE_MODE=downloads: the hosted service is /get and nothing playable."""

    @classmethod
    def setUpClass(cls):
        import api
        cls.client = api.app.test_client()

    def test_playable_routes_are_gone_and_the_site_remains(self):
        with patch.dict("os.environ", {"SITE_MODE": "downloads"}),              patch.object(downloads, "latest_build", lambda force=False: downloads._empty()):
            r = self.client.get("/")
            self.assertEqual((r.status_code, r.headers.get("Location")), (302, "/get"))
            for path in ("/get", "/api/builds/latest", "/api/health"):
                self.assertEqual(self.client.get(path).status_code, 200, path)
            for path in ("/standalone", "/play", "/studio", "/lobby", "/api/keys", "/images/x.png"):
                self.assertEqual(self.client.get(path).status_code, 404, path)
            for path in ("/api/reset", "/api/choose", "/api/keys/custom"):
                self.assertEqual(self.client.post(path, json={}).status_code, 404, path)

    def test_without_the_mode_nothing_changes(self):
        with patch.dict("os.environ", {}, clear=False) as env:
            env.pop("SITE_MODE", None)
            self.assertEqual(self.client.get("/standalone").status_code, 200)


if __name__ == "__main__":
    unittest.main()
