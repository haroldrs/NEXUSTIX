import pytest

from scripts.locust_safety import load_settings


def settings(**overrides):
    values = {
        "NEXUSTIX_STAGING_HTTP": "1",
        "NEXUSTIX_LOCUST_USERS_URL": "http://127.0.0.1:18081",
        "NEXUSTIX_LOCUST_EMAIL": "synthetic@example.test",
        "NEXUSTIX_LOCUST_PASSWORD": "synthetic-password",
        "NEXUSTIX_LOCUST_EVENT_ID": "987654321",
    }
    values.update(overrides)
    return values


def test_locust_accepts_only_explicit_staging_loopback_targets():
    config = load_settings("http://localhost:18085", settings())
    assert config.users_url == "http://127.0.0.1:18081"
    assert config.event_id == 987654321


@pytest.mark.parametrize("host", ["http://localhost:8005", "http://192.168.1.20:18085", "https://localhost:18085"])
def test_locust_rejects_non_staging_purchase_target(host):
    with pytest.raises(ValueError):
        load_settings(host, settings())


def test_locust_requires_explicit_opt_in_and_staging_users_endpoint():
    with pytest.raises(ValueError, match="NEXUSTIX_STAGING_HTTP"):
        load_settings("http://localhost:18085", settings(NEXUSTIX_STAGING_HTTP="0"))
    with pytest.raises(ValueError, match="NEXUSTIX_LOCUST_USERS_URL"):
        load_settings("http://localhost:18085", settings(NEXUSTIX_LOCUST_USERS_URL="http://localhost:8001"))


def test_locust_settings_do_not_reveal_password_in_repr():
    config = load_settings("http://localhost:18085", settings())
    assert "synthetic-password" not in repr(config)
