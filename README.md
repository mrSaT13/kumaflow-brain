# KumaFlow Brain

Self-hosted music intelligence layer for your media server (Navidrome, Jellyfin, Emby, Lyrion).

Индексирует библиотеку, делает настоящий sonic-анализ реальных аудиофайлов
(темп, тональность, энергия, танцевальность, настроение — движок librosa,
файлы берутся с диска worker'а или стримом из Navidrome),
подтягивает тексты из открытых источников и генерирует ежедневные плейлисты.

## Структура

```
.
├── docker-compose.yml        # ДЕПЛОЙ на сервер: готовые образы из GHCR (без сборки)
├── .env.example              # единственный env для всего стека на сервере
├── deploy/
│   ├── docker-compose.yml    # локальная разработка: сборка из исходников
│   ├── Dockerfile.server     # backend + worker
│   └── Dockerfile.web        # frontend
├── bridge/                   # мост метаданных (MusicBrainz / Last.fm), опционально
├── .github/workflows/
│   └── docker-publish.yml    # CI: сборка и пуш образов в GHCR при пуше в main
├── server/                   # FastAPI + SQLAlchemy + RQ (Python 3.11)
└── web/                      # Next.js 14 (App Router) + TypeScript + TailwindCSS
```

Порты по умолчанию: `3000` — веб, `8000` — API, `8001` — мост (опционально), `5432` — Postgres, `6379` — Redis, `4533` — Navidrome (опционально).

---

## Вариант A — тестовый сервер (рекомендуется)

Образы собирает GitHub Actions, на сервере только `pull + up`.

### 1. Один раз: создать репозиторий и запушить

```bash
git clone https://github.com/mrSaT13/kumaflow-brain.git kumaflow
cd kumaflow
git add -A
git commit -m "KumaFlow Brain: github-ready deploy"
git branch -M main
git remote add origin https://github.com/mrSaT13/kumaflow-brain.git
git push -u origin main
```

После пуша открой вкладку **Actions** — должен позеленеть `docker-publish`.
Готовые образы появятся во вкладке **Packages**:

- `ghcr.io/<owner>/<repo>-backend:latest`
- `ghcr.io/<owner>/<repo>-web:latest`
- `ghcr.io/<owner>/<repo>-bridge:latest`

> Если пакеты не видны — в настройках репозитория `Settings → Actions → General → Workflow permissions`
> должно быть `Read and write permissions` (иначе CI не сможет опубликовать пакеты).

### 2. На сервере: установка (всё в одном `docker-compose.yml`, без `.env`)

```bash
git clone https://github.com/mrSaT13/kumaflow-brain.git kumaflow && cd kumaflow
nano docker-compose.yml   # вписать 3 вещи (см. ниже)
docker compose pull
docker compose up -d
docker compose ps
curl -s http://localhost:8000/api/health   # {"status":"ok"}
```

Что поменять в `docker-compose.yml` (всё помечено `CHANGE_ME`):

| Место | Значение |
|---|---|
| `POSTGRES_PASSWORD` (2 места: postgres + backend + worker) | длинный случайный пароль, один и тот же везде |
| `NAVIDROME_URL` | адрес твоего внешнего Navidrome. Если он на том же сервере — оставить `http://host.docker.internal:4533`; если на другой машине — `http://<ip>:4533` |
| `CORS_ORIGINS` | добавить origin, с которого открываешь фронт: `http://<ip-или-домен-сервера>:3000` |

Логин/пароль Navidrome в файл можно не писать — они задаются в веб-UI
(Настройки → Медиа-сервер) и хранятся в базе.

### Sonic-анализ: как worker добирается до mp3

По умолчанию ничего монтировать не нужно: worker тянет каждый трек стримом
из твоего Navidrome через Subsonic API (`download`) во временный файл,
считает признаки через librosa и удаляет временный файл. Медленнее, но
работает из коробки.

Быстрый вариант — примонтировать в `worker` ту же папку музыки, что у
Navidrome (только чтение), в тот же путь (`/music`):

```yaml
# в секции worker файла docker-compose.yml:
    environment:
      MUSIC_DIR: /music
    volumes:
      - /путь/к/музыке/на/хосте:/music:ro
```

Анализ идёт пачками по 200 треков за прогон (`ANALYSIS_MAX_TRACKS_PER_RUN`) —
повторные запуски «Sonic» продолжают с места остановки. Прогресс и ошибки
по каждому треку видны в логах задачи (Задачи и логи → логи).

Открыть: веб — `http://<сервер>:3000`, API — `http://<сервер>:8000/api/health`,
доки — `http://<сервер>:8000/api/docs`.

### 3. Обновление после нового пуша в main

CI пересоберёт образы сам. На сервере:

```bash
git pull && docker compose pull && docker compose up -d
```

> Репозиторий публичный: реальные пароли живут только в копии
> `docker-compose.yml` на сервере. Никогда не делай `git push` с сервера.

### 4. Опционально: мост метаданных (MusicBrainz / Last.fm)

```bash
docker compose --profile bridge up -d
```

Дальше **без файлов** — всё через веб-UI:

1. Открой `http://<сервер>:3000/settings`.
2. Раздел «Мост метаданных»: URL `http://bridge:8001` → «Проверить мост» → «Сохранить».
3. Ключ Last.fm (`LASTFM_API_KEY`) — единственное, что задаётся прямо в секции `bridge` в `docker-compose.yml` на сервере; без него мост работает только через MusicBrainz.

Проверка из консоли: `curl -s http://localhost:8001/api/bridge/health`.

---

## Вариант B — локальная разработка без Docker

### Backend

```bash
cd server
python -m venv .venv
.venv\Scripts\activate        # Windows; Linux/macOS: source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # в dev-режиме по умолчанию sqlite (DB_URL_OVERRIDE), Postgres не нужен
uvicorn app.main:app --reload --port 8000
```

Воркер — вторым терминалом:

```bash
cd server
.\.venv\Scripts\Activate.ps1
python -m app.workers.rq_worker
```

### Frontend

```bash
cd web
npm install
npm run dev    # http://localhost:3000
```

### Локально через Docker (сборка из исходников)

```bash
cp server/.env.example server/.env
cd deploy
docker compose up -d --build
```

---

## Переменные окружения

| Файл | Назначение |
|---|---|
| `.env` (корень) | весь стек в `docker-compose.yml` на сервере |
| `server/.env` | только локальный запуск без Docker и `deploy/docker-compose.yml` |
| `web/.env.local` | только локальный `npm run dev` (по умолчанию не нужен — работает прокси `/api → backend`) |

В контейнерах Postgres используется принудительно (`DB_URL_OVERRIDE=""`),
sqlite-файл в Docker не используется — данные живут в volume `pgdata`.

## Полезные команды на сервере

```bash
docker compose logs -f backend worker web   # логи
docker compose ps                           # статус
docker compose down                         # остановить (данные в volumes сохранятся)
docker compose down -v                      # остановить И удалить данные (осторожно!)
```

## Лицензия

Copyright (C) 2026 mrSaT13.

Этот проект распространяется под лицензией **GNU Affero General Public License v3.0
(AGPL-3.0)** — см. файл [LICENSE](LICENSE). Если вы запускаете изменённую версию
на сервере, вы обязаны предоставить пользователям исходный код (раздел 13 AGPL).
