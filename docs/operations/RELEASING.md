# Releasing ABYSS

How a build gets from `main` to a player's machine, and how it updates itself
there. The design record is `docs/plans/DISTRIBUTION_MVP_PLAN.md`; this is the
procedure.

## The shape of it

```
main ──tag v0.2.0-beta.1──▶ .github/workflows/release.yml
                             gate · stamp · build · smoke (read-only) · notices
                             · Velopack pack · sign · publish
                                              │
                          github.com/mradfo21/abyss-releases (public, builds only)
                              │                                 │
                     /get serves *-Setup.exe          every installed copy checks
                     (downloads.py)                   at launch (updater.py)
```

- **Source** is `mradfo21/5th_Corner_Dev`. **Builds** go to
  `mradfo21/abyss-releases`, which holds a README and Releases, never code.
- **Versions** are SemVer tags on `main`. A tag with a pre-release part
  (`v0.2.0-beta.1`) ships on the **beta** channel as a GitHub pre-release; a
  plain one (`v0.2.0`) ships on **stable**. Velopack keeps each install on the
  channel it came from.
- **Identity** is `app_identity.py`: `ABYSS`, package id `5thCorner.ABYSS`,
  player data in `%APPDATA%\ABYSS`. The package id and data folder can never
  change once anyone has installed.

## Cutting a release

1. Everything is merged to `main` and CI (`.github/workflows/ci.yml`) is green.
2. Add the release notes to the top of `CHANGELOG.md` (the release body is
   taken from the tag's annotation; `/get` shows its `##` headings as
   "what's new").
3. Tag and push:

   ```bash
   git tag -a v0.2.0-beta.1 -m "ABYSS 0.2.0-beta.1"
   git push origin v0.2.0-beta.1
   ```

4. Watch the **release** workflow. With `RELEASES_TOKEN` set it publishes;
   without it the packed build is kept as the run's artifact.
5. Check the release on `abyss-releases`: `ABYSS-win-Setup.exe`,
   `ABYSS-win-Portable.zip`, the `.nupkg` packages and `releases.*.json`.
6. Install it in Windows Sandbox from `/get` (the clean-machine test in the
   plan's "Done means").

### Without CI

```bash
python tools/release_local.py v0.2.0-beta.1 --publish
```

Same steps on this machine; publishes with `gh auth token`. Without
`--publish` it only packs into `Releases/`.

## Rehearsing an update

```bash
python tools/release_local.py v0.2.0-beta.1 --skip-gate
python tools/release_local.py v0.2.0-beta.2 --skip-gate
```

Both land in `Releases/`. Install the first from `Releases/*-Setup.exe` with
`ABYSS_UPDATE_REPO` set to the `Releases` folder (and `APPDATA` pointed at a
scratch folder so the run does not touch your own keys). The start menu offers
**UPDATE READY — RESTART** within a minute; pressing it restarts into the
second, and `/api/health` reports its version.

## Secrets (source repo → Settings → Secrets and variables → Actions)

| Secret | What | Without it |
|---|---|---|
| `RELEASES_TOKEN` | Fine-grained PAT, **Contents: read and write** on `mradfo21/abyss-releases` only | The build is kept as a workflow artifact, not published |
| `AZURE_TENANT_ID`, `AZURE_CLIENT_ID`, `AZURE_CLIENT_SECRET` | Service principal allowed to sign | Unsigned: SmartScreen says "unknown publisher" |
| `AZURE_SIGN_ENDPOINT`, `AZURE_SIGN_ACCOUNT`, `AZURE_SIGN_PROFILE` | The Artifact Signing account and certificate profile | Same |

No game API key is ever a CI secret: the gate and the smoke test run in mock
mode.

## What a player's machine holds

| Where | What | An update… |
|---|---|---|
| `%LOCALAPPDATA%\5thCorner.ABYSS\current\` | The program | Replaces it |
| `%APPDATA%\ABYSS\` | Keys, account, characters, sessions, tapes, Worlds they made, logs | Never touches it |

Factory files the game also rewrites (the live prompt file, `ai_config.json`,
`pricing.json`, the shipped Worlds) are seeded into `%APPDATA%\ABYSS` and
refreshed by an update only while the player's copy is still the factory copy
(`paths.seed_factory`, `.factory.json`). Uninstalling removes the program and
leaves `%APPDATA%\ABYSS`.
