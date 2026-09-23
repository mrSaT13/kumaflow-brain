"""Сейф паролей Navidrome для opt-in автообновления вкусов.

Шифр Fernet. Ключ — приоритетно env TASTE_VAULT_KEY (секрет compose),
иначе автогенерированный в БД (AppSetting.taste_vault_key, создаётся сам
по первому нажатию «Запомнить пароль» — в веб лазить никуда не надо).

Трейдофф: ключ в БД лежит рядом с шифротекстами — при утечке базы пароли
восстановимы. Для домашней LAN приемлемо; env-ключ остаётся строже
(приоритетнее) для параноиков.
"""
from __future__ import annotations

from app.core.config import get_settings
from app.core.logging import get_logger

logger = get_logger("vault")

VAULT_SETTING_KEY = "taste_vault_key"


def _env_key() -> str:
    try:
        return (get_settings().taste_vault_key or "").strip()
    except Exception:
        return ""


def _db_key() -> str:
    try:
        from app.db.database import session_scope
        from app.db.models import AppSetting

        with session_scope() as db:
            row = db.get(AppSetting, VAULT_SETTING_KEY)
            v = row.value if row is not None else None
            if isinstance(v, dict):
                return str(v.get("key") or "").strip()
            return str(v or "").strip()
    except Exception:
        return ""


def key_source() -> str:
    """env | db | none — откуда ключ (для бейджа в UI, без самого ключа)."""
    if _env_key():
        return "env"
    if _db_key():
        return "db"
    return "none"


def ensure_vault_key() -> tuple[str, bool]:
    """Вернуть ключ, при отсутствии — сгенерировать в БД. (key, created)."""
    env = _env_key()
    if env:
        return env, False
    existing = _db_key()
    if existing:
        return existing, False
    new = generate_key()
    from app.db.database import session_scope
    from app.db.models import AppSetting

    with session_scope() as db:
        row = db.get(AppSetting, VAULT_SETTING_KEY)
        payload = {"key": new, "auto": True}
        if row is None:
            db.add(AppSetting(key=VAULT_SETTING_KEY, value=payload))
        else:
            row.value = payload
        db.commit()
    logger.info("vault key auto-generated in DB (first password remember)")
    return new, True


def vault_available() -> bool:
    try:
        return bool(_env_key() or _db_key())
    except Exception:
        return False


def _fernet():
    from cryptography.fernet import Fernet

    raw = _env_key() or _db_key()
    if not raw:
        raise RuntimeError(
            "Нет ключа сейфа — нажмите «Запомнить пароль» ещё раз "
            "(ключ создастся сам)."
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
