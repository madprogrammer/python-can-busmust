# Releasing

GitHub Actions runs tests and packaging checks on pushes and pull requests.
The matrix covers Linux (Python 3.9 and 3.14), macOS and Windows (Python 3.12),
plus the minimum supported python-can/PyUSB versions. CI retains its wheel and
source distributions as downloadable workflow artifacts for 14 days.

## Publish a version

1. Update `project.version` in `pyproject.toml` and `__version__` in
   `src/busmust/__init__.py`. Update the README and changelog as appropriate.
   Continue using alpha versions, such as `0.1.0a2`, until hardware testing
   supports changing the quality designation.
2. Commit and push the changes, then publish a GitHub release tagged with the
   exact `v<version>`, for example `v0.1.0a2`. Mark alpha/beta/RC versions as
   **prereleases**. The tag must point to the commit containing that version.
3. The `Publish release` workflow validates the tag and prerelease flag, runs
   the full CI matrix on the resolved commit, builds a wheel and source archive,
   checks installation/plugin registration, and uploads both to PyPI. It then
   verifies their PyPI hashes and attaches the same files to the GitHub release.

For example, after committing and pushing the version change:

```sh
gh release create v0.1.0a2 --target main --prerelease \
  --title '0.1.0a2' --notes-file /path/to/release-notes.md
```

Publishing a GitHub draft, or pushing a tag without publishing a GitHub release,
does not upload to PyPI. The `published` event handles both prereleases and
stable releases. Existing PyPI version files cannot be replaced.

## Credentials

The `pypi` GitHub environment contains `PYPI_API_TOKEN`. Only the PyPI upload
job receives it; builds, tests, pull requests, and dry runs do not. The
environment accepts deployments from `v*` tags only. Update the token with:

```sh
gh secret set PYPI_API_TOKEN --env pypi < /path/to/token-only-file
```

The local `pypi-creds.txt` is ignored by Git and is not part of distributions.

This setup uses API-token authentication, not Trusted Publishing. A future
tokenless migration requires adding a trusted publisher in PyPI for:

- GitHub owner: `madprogrammer`
- Repository: `python-can-busmust`
- Workflow filename: `release.yml`
- Environment: `pypi`

After configuring PyPI, grant `id-token: write` to the `pypi` job, remove its
`password` input and `attestations: false` setting, and remove the unused token
secret. See [PyPI's Trusted Publishing guide](https://docs.pypi.org/trusted-publishers/using-a-publisher/).

## Dry runs and retries

To validate an existing release tag without modifying PyPI or GitHub releases:

```sh
gh workflow run release.yml --ref main -f tag=v0.1.0a1
```

Manual dispatch always runs in dry-run mode. It runs the validation, test matrix,
build, and wheel-installation checks and retains `release-distributions` for
30 days. Both publishing jobs are skipped, and the token is not accessed.

If an actual release upload fails transiently, use **Re-run failed jobs** on
the existing run. This reuses the original built distributions. Already-uploaded
PyPI files are skipped, and the final hash check prevents replacing GitHub assets
with files that differ from PyPI. Rerunning the entire workflow can regenerate
different archive bytes; if that happens, the hash check deliberately fails.
Download the original `release-distributions` artifact for recovery rather than
overwriting an existing version with a different build.

If the version/tag check or tests fail, fix the source and create a new version
and release. Automated checks use simulated USB hardware; a green workflow does
not establish real-adapter compatibility.
