"""Проверка обновлений мозга с GitHub + уведомление в колокольчик.

Как работает:
- раз в день крон `check_updates` тянет `config.py` с main-ветки и сравнивает
  `app_version` со своей. Нашёл новее — пишет `notify()` (ссылка /settings).
- кнопка «Проверить обновления» в «О программе» — та же проверка, но сразу.
- чтобы не спамить каждый день, версия последнего уведомления хранится
  в `AppSetting(updates:last_notified)`.

Теги/релизы на GitHub не нужны — сравниваем строки версий.
Нет сети / GitHub лежит — тихо возвращаем «неизвестно», ничего не валим.
"""
from __future__ import annotations

import re
from typing import Any

GITHUB_RAW_CONFIG = (
    "https://raw.githubusercontent.com/mrSaT13/kumaflow-brain"
    "/main/server/app/core/config.py"
)
NOTIFIED_KEY = "updates:last_notified"
FETCH_TIMEOUT_SEC = 15

_VERSION_RE = re.compile(r'app_version\s*:\s*str\s*=\s*"([^"]+)"')


def parse_version(text: str | None) -> str | None:
    """Вытащить app_version из текста config.py. Нет совпадения -> None."""
    if not text:
        return None
    m = _VERSION_RE.search(text)
    return m.group(1).strip() if m else None


def _parts(v: str) -> tuple:
    out = []
    for p in str(v or "").strip().split("."):
        m = re.match(r"(\d+)", p)
        out.append(int(m.group(1)) if m else 0)
    return tuple(out)


def is_newer(latest: str | None, current: str | None) -> bool:
    """latest строго новее current. Что-то не распарсилось -> False."""
    if not latest or not current:
        return False
    try:
        return _parts(latest) > _parts(current)
    except Exception:
        return False


def fetch_latest(timeout: int = FETCH_TIMEOUT_SEC) -> str | None:
    """Версия с main-ветки GitHub. Нет сети/ошибка -> None (не валим)."""
    try:
        import urllib.request

        req = urllib.request.Request(
            GITHUB_RAW_CONFIG, headers={"User-Agent": "kumaflow-brain"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read(200_000).decode("utf-8", "replace")
        return parse_version(raw)
    except Exception:
        return None


def current_version() -> str:
    try:
        from app.core.config import get_settings

        return str(get_settings().app_version)
    except Exception:
        return "?"


def check() -> dict[str, Any]:
    """Сверить свою версию с GitHub. Сеть не трогаем лишнего — один GET."""
    cur = current_version()
    latest = fetch_latest()
    return {
        "current": cur,
        "latest": latest,
        "update_available": is_newer(latest, cur),
    }


def check_and_notify(db) -> dict[str, Any]:
    """Проверка + уведомление один раз на версию. Возвращает check() + notified."""
    res = check()
    notified = False
    try:
        if res.get("update_available") and res.get("latest"):
            from app.db.models import AppSetting
            from app.services import notify as _notify

            row = db.get(AppSetting, NOTIFIED_KEY)
            seen = str(row.value) if row is not None and row.value else ""
            if seen != str(res["latest"]):
                _notify.notify(
                    db, "info",
                    f"Доступно обновление мозга: {res['latest']}",
                    f"Сейчас стоит {res['current']}. "
                    f"Что нового — Настройки → О программе.",
                    link="/settings",
                )
                try:
                    _notify.prune(db)
                except Exception:
                    pass
                if row is None:
                    db.add(AppSetting(key=NOTIFIED_KEY, value=str(res["latest"])))
                else:
                    row.value = str(res["latest"])
                db.commit()
                notified = True
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass
    res["notified"] = notified
    return res


def local_changelog() -> str:
    """Текст CHANGELOG.md из корня репо. Нет файла -> пусто (веб покажет фолбек)."""
    try:
        from pathlib import Path

        p = Path(__file__).resolve().parents[3] / "CHANGELOG.md"
        return p.read_text(encoding="utf-8") if p.exists() else ""
    except Exception:
        return ""
