"""Fail-fast на секреты-плейсхолдеры из публичного docker-compose.yml."""
import pytest

from app.core.config import Settings


def test_placeholder_token_rejected():
    with pytest.raises(ValueError, match="BRAIN_API_TOKEN"):
        Settings(brain_api_token="CHANGE_ME_brain_token")


def test_placeholder_postgres_rejected():
    with pytest.raises(ValueError, match="POSTGRES_PASSWORD"):
        Settings(env="prod", postgres_password="CHANGE_ME_postgres_password")


def test_multiple_placeholders_listed_together():
    with pytest.raises(ValueError, match="BRAIN_API_TOKEN.*TASTE_VAULT_KEY"):
        Settings(
            brain_api_token="CHANGE_ME_brain_token",
            taste_vault_key="CHANGE_ME_taste_vault_key",
        )


def test_defaults_ok_empty_means_disabled():
    s = Settings()
    assert s.brain_api_token == ""
    assert s.taste_vault_key == ""


def test_prod_with_real_secrets_ok():
    s = Settings(
        env="prod",
        postgres_password="s3cure-random-password",
        brain_api_token="a" * 64,
        taste_vault_key="",
    )
    assert s.env == "prod"


def test_non_secret_field_with_marker_does_not_raise():
    s = Settings(cors_origins=["http://CHANGE_ME_server_ip:3000"])
    assert "CHANGE_ME_server_ip" in s.cors_origins[0]
