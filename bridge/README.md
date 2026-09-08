# KumaFlow Bridge

Кэширующий шлюз к MusicBrainz и Last.fm для сервера генерации.

**Идея:** внешние API имеют жёсткие лимиты (MusicBrainz ~1 запрос/сек, Last.fm ~5/сек).
Мост держит лимиты сам, кэширует ответы в памяти с TTL и отдаёт backend единый
нормализованный формат — backend изолирован от изменений внешних API.

**Статус:** заготовка v0. Без БД: кэш живёт в памяти до рестарта. Этого достаточно,
чтобы поднять, проверить связку и начать обогащать метаданные. Позже: Redis-кэш
и таблица `artist_metadata` на стороне backend.

## Эндпоинты

| Метод | Путь | Что делает |
|---|---|---|
| GET | `/api/bridge/health` | пинг + какие провайдеры активны |
| GET | `/api/bridge/artist?name=Radiohead` | артист: mbid, страна, теги, похожие, био |
| GET | `/api/bridge/release?artist=...&album=...` | первый лучший матч релиза в MusicBrainz |

Без `LASTFM_API_KEY` мост работает в урезанном режиме (только MusicBrainz) — это не ошибка.

## Настройка без файлов

Адрес моста задаётся в веб-UI: **Настройки → Мост метаданных** (URL + вкл/выкл + «Проверить мост»).
Сохраняется в базе backend, бэкенд подхватывает сам. Перезапуск не нужен.

## Локальный запуск

```bash
cd bridge
cp .env.example .env   # вписать LASTFM_API_KEY (опционально)
uvicorn main:app --port 8001
```

## В Docker Compose (профиль `bridge`)

```bash
# на сервере: образы уже собраны CI
docker compose --profile bridge up -d

# локально из исходников
cd deploy && docker compose --profile bridge up -d --build
```

Затем в веб-UI: Настройки → Мост → URL `http://bridge:8001` (в compose) или
`http://localhost:8001` (локально) → «Проверить мост» → «Сохранить».
