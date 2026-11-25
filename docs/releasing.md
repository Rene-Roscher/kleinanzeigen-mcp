# Releasing

Releases are published to PyPI by `.github/workflows/release.yml` when a tag `v<version>` is pushed.
It uses [trusted publishing](https://docs.pypi.org/trusted-publishers/) – no API token is stored anywhere.

## One-time setup

1. Create an account on [pypi.org](https://pypi.org) (enable 2FA).
2. Go to **Your account → Publishing → Add a new pending publisher → GitHub** and enter:

   | Field | Value |
   |---|---|
   | PyPI project name | `kleinanzeigen-mcp` |
   | Owner | `Rene-Roscher` |
   | Repository name | `kleinanzeigen-mcp` |
   | Workflow name | `release.yml` |
   | Environment name | `pypi` |

3. Optional: in the GitHub repository under **Settings → Environments → pypi**, add yourself as a required
   reviewer so every upload waits for a click. The environment is created automatically on the first release.

## Cutting a release

1. Bump `version` in `pyproject.toml`, `src/kleinanzeigen_mcp/__init__.py`, `api.py` and `mcp_server.py`.
2. Run `uv lock` and add a `## [x.y.z] – YYYY-MM-DD` section to `CHANGELOG.md`.
3. Commit, then tag and push:

   ```bash
   git tag v0.2.1
   git push origin main v0.2.1
   ```

The workflow runs the full CI (Linux, macOS, Windows, package, Docker), checks that the tag matches the version
and that the changelog has an entry, builds sdist and wheel, uploads them to PyPI and creates a GitHub release
with the changelog section as notes.

Note: everything published to PyPI is public, including the source distribution – even if the repository is private.
