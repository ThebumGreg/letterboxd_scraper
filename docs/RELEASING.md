# Releasing SyncHub

How a change gets from this repository to your Unraid server.

```
branch ──► pull request (tests + image build) ──► main ──► GitHub Actions: tests, then publish ──► ghcr.io/thebumgreg/synchub:latest
                                                    │
                                          tag v1.2.3 ──► also :1.2
                                                                                  Unraid: Docker tab ► Check for Updates ► apply update
```

## Day to day

1. Work on a branch. Every push and pull request runs the tests and builds the image (`.github/workflows/ci.yml`), so a broken Dockerfile or a failing test shows up before `main`.
2. Merge to `main`. `.github/workflows/docker-publish.yml` runs the tests again and publishes `:latest`, `:<version>` (the number in `synchub/__init__.py`, for example `:1.1.0`) and `:sha-<commit>`. Unraid's *Check for Updates* then shows SyncHub as *update ready*. `:1.1.0` moves with every merge until the number is raised, so it is "the newest 1.1.0", not a frozen build; `:sha-<commit>` is the frozen one.

## Making a numbered release

1. Change `__version__` in `synchub/__init__.py` (for example `1.2.0`; the version is shown at the bottom of every page and is the image's label).
2. Add a section to `CHANGELOG.md`.
3. Commit, merge to `main`, wait for the publish run to go green (GitHub, *Actions*). That publishes `:1.2.0` and `:latest`.
4. Optional: tag it so GitHub lists a release: `git tag v1.2.0 && git push origin v1.2.0` (or *Releases, Draft a new release* on GitHub with a new tag `v1.2.0`). The workflow then also publishes `:1.2`.

Containers that follow `:latest` update on the next check; containers pinned to `:1.2.0` follow that version's newest build, and `:sha-<commit>` never changes.

## Database upgrades

The app upgrades its own database on start (numbered files in `synchub/migrations/`, applied in order, once) and saves a copy first as `/data/backups/premigrate-v<N>-<time>.db`. A new migration is a new file with the next number; never edit one that has shipped. Going *back* to an older image after an upgrade is not supported: restore a backup made before it (DEPLOY.md, section 6).

## First time only

- After the first publish, check the package on GitHub (your profile, *Packages*, `synchub`). A package from a private repository is private: make it public or log Unraid in (DEPLOY.md, section 1).
- *Settings, Actions, General*: "Workflow permissions" may stay at the default; the workflow asks for `packages: write` itself.

## If a publish fails

*Actions* shows which step. The usual causes: a test that fails (fix it; nothing is published), a package name the Dockerfile's `apt-get` cannot find (the Dockerfile tries `adb` and then `android-tools-adb`), or the package being linked to a different repository (package settings, *Manage Actions access*, add this repository with *Write*).
