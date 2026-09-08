# KumaFlow Brain

Self-hosted music intelligence layer for your media server (Navidrome, Jellyfin, Emby, Lyrion).

Индексирует библиотеку, делает sonic-анализ (темп, тональность, настроение, CLAP-эмбеддинги),
группирует треки в кластеры, подтягивает тексты из открытых источников, обогащает метаданные
через Yandex.Music, строит коллаборативные профили и генерирует ежедневные плейлисты.

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

### 2. На сервере: установка

```bash
git clone https://github.com/mrSaT13/kumaflow-brain.git kumaflow && cd kumaflow
cp .env.example .env
nano .env   # вписать BACKEND_IMAGE, WEB_IMAGE, POSTGRES_PASSWORD, CORS_ORIGINS
docker compose pull
docker compose up -d
docker compose ps
curl -s http://localhost:8000/api/health   # {"status":"ok"}
```

Что вписать в `.env`:

| Переменная | Значение |
|---|---|
| `BACKEND_IMAGE` | `ghcr.io/<owner>/<repo>-backend:latest` из вкладки Packages |
| `WEB_IMAGE` | `ghcr.io/<owner>/<repo>-web:latest` из вкладки Packages |
| `BRIDGE_IMAGE` | `ghcr.io/<owner>/<repo>-bridge:latest` (только если нужен профиль `bridge`) |
| `POSTGRES_PASSWORD` | длинный случайный пароль |
| `CORS_ORIGINS` | `["http://localhost:3000"]` + origin, с которого открываешь фронт (IP/домен сервера) |
| `NAVIDROME_URL/USER/PASSWORD` | доступ к медиа-серверу |

Открыть: веб — `http://<сервер>:3000`, API — `http://<сервер>:8000/api/health`,
доки — `http://<сервер>:8000/api/docs`.

### 3. Обновление после нового пуша в main

CI пересоберёт образы сам. На сервере:

```bash
docker compose pull && docker compose up -d
```

### 4. Опционально: Navidrome рядом в том же compose

```bash
mkdir -p deploy/music   # положить сюда музыку
docker compose --profile media up -d
```

### 5. Опционально: мост метаданных (MusicBrainz / Last.fm)

```bash
docker compose --profile bridge up -d
```

Дальше **без файлов** — всё через веб-UI:

1. Открой `http://<сервер>:3000/settings`.
2. Раздел «Мост метаданных»: URL `http://bridge:8001` → «Проверить мост» → «Сохранить».
3. Ключ Last.fm (`LASTFM_API_KEY`) — единственное, что задаётся в `.env` на стороне моста; без него мост работает только через MusicBrainz.

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
