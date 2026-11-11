"""Test isolation: never read the machine's real ~/.config/venue-booking config.

Without this, the suite would behave differently on a machine that has a real
config filled in (the site/crypto would use the real values instead of the
generic placeholders the tests assume). We point the user-config path at a
nonexistent temp file and clear $VB_CONFIG, so ``config.config_path()`` returns
``None`` and the code falls back to its placeholder site/venues. Tests that need
a specific config set ``VB_CONFIG`` themselves, which still wins.
"""

import pytest

import config


@pytest.fixture(autouse=True)
def _isolate_user_config(monkeypatch, tmp_path):
    monkeypatch.delenv("VB_CONFIG", raising=False)
    monkeypatch.setattr(config, "USER_CONFIG", tmp_path / "no-user-config.toml")
    config._warned.clear()
