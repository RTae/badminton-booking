import os
import stat
import tomllib

import pytest

import browser_login, config

TEMPLATE_SHAPE = {
    "site": {"base_url": "", "sso_host": "", "app_id": "", "sign_key": ""},
    "venues": {"main": {"name": "", "scene_uuid": "", "dev_kind_uuid": "", "class_type_uuid": ""}},
    "sso": {"username": "", "password": ""},
    "booking": {"times": ["12:00"], "courts": [4, 5]},
}


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    """No real config / Keychain: point every lookup somewhere empty."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("VB_CONFIG", raising=False)
    monkeypatch.setattr(config, "USER_CONFIG", tmp_path / "home" / "config.toml")
    monkeypatch.setattr(browser_login, "keychain_credentials", lambda: None)
    config._warned.clear()


def write(path, text, mode=0o600):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(mode)
    return path


def test_no_config_means_no_credentials():
    assert config.config_path() is None
    assert config.sso_credentials() is None
    assert browser_login.load_credentials() is None


def test_reads_user_config_with_special_characters():
    write(config.USER_CONFIG, "[sso]\nusername = '2025123456'\npassword = 'p@ss\"wo\\rd#1'\n")
    assert config.sso_credentials() == ("2025123456", 'p@ss"wo\\rd#1')
    assert browser_login.load_credentials() == ("2025123456", 'p@ss"wo\\rd#1')
    assert browser_login.credentials_source().startswith("config file")


def test_local_config_beats_user_config_and_env_beats_both(tmp_path, monkeypatch):
    write(config.USER_CONFIG, "[sso]\nusername='home'\npassword='h'\n")
    write(tmp_path / "config.toml", "[sso]\nusername='local'\npassword='l'\n")
    assert config.sso_credentials() == ("local", "l")
    other = write(tmp_path / "elsewhere.toml", "[sso]\nusername='env'\npassword='e'\n")
    monkeypatch.setenv("VB_CONFIG", str(other))
    assert config.sso_credentials() == ("env", "e")


def test_config_beats_keychain(monkeypatch):
    monkeypatch.setattr(browser_login, "keychain_credentials", lambda: ("kc", "kc-pw"))
    assert browser_login.load_credentials() == ("kc", "kc-pw")
    write(config.USER_CONFIG, "[sso]\nusername='cfg'\npassword='cfg-pw'\n")
    assert browser_login.load_credentials() == ("cfg", "cfg-pw")


def test_empty_template_counts_as_not_set():
    config.create(config.USER_CONFIG)
    assert config.sso_credentials() is None


def test_half_filled_is_an_error():
    write(config.USER_CONFIG, "[sso]\nusername='2025123456'\npassword=''\n")
    with pytest.raises(config.ConfigError, match="both"):
        config.sso_credentials()


def test_bad_toml_is_a_clear_error():
    write(config.USER_CONFIG, "[sso]\nusername = 2025\"oops\n")
    with pytest.raises(config.ConfigError, match="not valid TOML"):
        config.sso_credentials()


def test_missing_env_path_is_an_error(monkeypatch, tmp_path):
    monkeypatch.setenv("VB_CONFIG", str(tmp_path / "nope.toml"))
    with pytest.raises(config.ConfigError, match="does not exist"):
        config.sso_credentials()


def test_warns_once_when_others_can_read(capsys):
    write(config.USER_CONFIG, "[sso]\nusername='a'\npassword='b'\n", mode=0o644)
    config.sso_credentials()
    config.sso_credentials()
    err = capsys.readouterr().err
    assert err.count("chmod 600") == 1


def test_private_file_does_not_warn(capsys):
    write(config.USER_CONFIG, "[sso]\nusername='a'\npassword='b'\n", mode=0o600)
    config.sso_credentials()
    assert capsys.readouterr().err == ""


def test_create_is_private_valid_and_never_overwrites():
    path = config.create(config.USER_CONFIG)
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert tomllib.loads(path.read_text()) == TEMPLATE_SHAPE
    path.write_text("[sso]\nusername='mine'\npassword='keep'\n")
    with pytest.raises(FileExistsError):
        config.create(config.USER_CONFIG)
    assert "keep" in path.read_text()


def test_example_file_in_repo_matches_template_shape():
    example = os.path.join(os.path.dirname(__file__), "..", "config.example.toml")
    with open(example, "rb") as f:
        assert tomllib.load(f) == TEMPLATE_SHAPE
