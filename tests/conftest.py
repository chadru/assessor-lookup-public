"""Shared test configuration.

Every test runs with a hermetic per-user config directory so the developer's
real ~/.config/assessor-lookup/ (registry cache, onboarded cases, goldens)
can never leak into — or be modified by — the suite.
"""

import pytest


@pytest.fixture(autouse=True)
def _hermetic_user_config(tmp_path, monkeypatch):
    monkeypatch.setenv("ASSESSOR_LOOKUP_HOME", str(tmp_path / "user-config"))
