# Release checks

Updates must preserve saved commutes and Home Assistant entities. A changed entity ID can break a user's dashboard or automation.

1. Run the commands in [.github/workflows/tests.yml](../.github/workflows/tests.yml).
2. Confirm `tests_ha/test_localization.py` passes its saved entity ID list in English and Dutch.
3. Investigate changed IDs before editing the golden list. Existing IDs need a migration if their meaning changes.
4. Keep the package version, lockfile and integration manifest version and requirement aligned.
5. Confirm the matching core package is available on PyPI before promoting the Home Assistant release.
6. Verify the release image supports both amd64 and arm64.

For catalog submission, HACS and Hassfest must pass without ignored checks before publishing a full release. Follow the [HACS publishing requirements](https://www.hacs.xyz/docs/publish/include).
