import pytest

# Bind `custom_components` to this repo first: the phcc test config folder ships its own regular
# `custom_components` package that would otherwise shadow ours and hide the integration.
import custom_components.train_commute_companion  # noqa: F401


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    yield
