"""Pytest fixtures for Assist Harness."""

from pathlib import Path

import pytest

import pytest_homeassistant_custom_component.common as common

pytest_plugins = "pytest_homeassistant_custom_component"

_REPO = Path(__file__).resolve().parents[1]


def _config_dir(*parts):
    """Point Home Assistant's test config at this repository."""
    return str(_REPO.joinpath(*parts))


common.get_test_config_dir = _config_dir


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Load custom_components/assist_harness from the repository."""
    yield
