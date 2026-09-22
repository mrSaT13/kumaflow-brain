"""Сейф паролей Navidrome для opt-in автообновления вкусов.

Шифр Fernet (cryptography уже в зависимостях). Ключ — ТОЛЬКО в env
TASTE_VAULT_KEY (секрет compose, генерируется один раз, в базе его нет).
Без ключа записи невозможны: store вернёт ошибку с подсказкой.
"""
from __future__ import annotations

from app.core.config import get_settings
from app.core.logging import get_logger

logger = get_logger("vault")


def vault_available() -> bool:
    try:
        return bool((get_settings().taste_vault_key or "").strip())
    except Exception:
        return False


def _fernet():
    from cryptography.fernet import Fernet

    raw = (get_settings().taste_vault_key or "").strip()
    if not raw:
        raise RuntimeError(
            "TASTE_VAULT_KEY не задан — автообновление вкусов выключено. "
            "Сгенерируйте: python -c \"from cryptography.fernet import Fernet; "
            "print(Fernet.generate_key().decode())\" и впишите в compose/backend+worker."
        )
    try:
        return Fernet(raw.encode())
    except Exception as e:
        raise RuntimeError(f"TASTE_VAULT_KEY битый ({e}) — сгенерируйте новый") from e


def encrypt_password(plain: str) -> bytes:
    return _fernet().encrypt(plain.encode("utf-8"))


def decrypt_password(blob: bytes) -> str:
    return _fernet().decrypt(bytes(blob)).decode("utf-8")


def generate_key() -> str:
    from cryptography.fernet import Fernet

    return Fernet.generate_key().decode()
