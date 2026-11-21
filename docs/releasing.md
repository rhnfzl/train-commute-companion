# Release checks

Updates must preserve saved commutes and Home Assistant entities. A changed entity ID can break a user's dashboard or automation.

1. Run the commands in [.github/workflows/tests.yml](../.github/workflows/tests.yml).
2. Confirm `tests_ha/test_localization.py` passes its saved entity ID list in English and Dutch.
3. Investigate changed IDs before editing the golden list. Existing IDs need a migration if their meaning changes.
4. Keep the package version, lockfile and integration manifest version and requirement aligned.
5. Confirm the matching core package is available on PyPI before promoting the Home Assistant release.
6. Verify the release image supports both amd64 and arm64.

For catalog submission, HACS and Hassfest must pass without ignored checks before publishing a full release. Follow the [HACS publishing requirements](https://www.hacs.xyz/docs/publish/include).

## Publish packages before the stable release

Home Assistant must find the matching core package when a user installs the release.

1. Create the version tag from the reviewed, green main commit.
2. Publish its GitHub release as a prerelease first.
3. Wait for the Release workflow's tests, PyPI job and Docker job.
4. Check that PyPI's version endpoint lists both the wheel and source archive.
5. Inspect the versioned image and confirm amd64 and arm64 support.
6. Promote that same GitHub release to stable after both checks pass.
7. Dispatch the Release workflow with that existing tag to update Docker's shared tags.
8. Confirm the versioned, minor-series and latest image tags have the intended digest.

The workflow runs on `release.published`. Its manual dispatch builds images for an existing release and does not publish to PyPI.
Keep the tag and package version unchanged during promotion. HACS catalog submission follows the stable release and publication checks.
