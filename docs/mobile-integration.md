# Заметка: что нужно мобильному клиенту для работы с новыми мозгами

Дата: 2026-09-22. Мобилу кодом не трогаем — это ТЗ для того, кто будет доделывать клиент завтра.

## Идея интеграции

Доп. интеграция на клиенте, включается **тумблером в настройках + ввод URL сервера**
(например `http://192.168.1.10:8000`). По умолчанию ВЫКЛ — мобила работает как сейчас,
полностью локально. Включил + ввёл URL — клиент начинает общаться с мозгом.

Важно: аутентификации на мозге сейчас **нет** (доверенная LAN). Перед выходом наружу нужен
токен/ключ — отдельная задача, не забыть.

## Соответствие сущностей

- `user_id` мозга = uuid из `POST /api/users/by-credentials` или `/api/users/` (создать один раз,
  сохранить на клиенте). Либо слать свой `external_id` туда, где endpoint его принимает.
- Трек: мобильный id (Navidrome song id) = серверный `Track.external_id`.
  Сервер сам резолвит оба формата почти везде (`track_id` = наш uuid **или** external_id).
  Ответы сервера всегда в наших uuid + `title/artist` — для показа достаточно, для
  воспроизведения клиент маппит обратно через external_id.
- Артист на сервере идентифицируется **именем** (`artist_name`), не id — учитывать в банах.

## Что вызывать и когда (минимум)

1. **После холодного старта (визард)** — один раз:
   `POST /api/users/{id}/sync-from-mobile`
   ```json
   {
     "ratings": [{"external_id": "...", "like": true, "playCount": 0, "skipCount": 0,
                  "replayCount": 0, "seekBackCount": 0, "abandonCount": 0,
                  "score": 100, "lastPlayed": "2026-09-22T10:00:00"}],
     "profile": {"preferredGenres": {}, "preferredArtists": {},
                 "likedSongs": ["ext..."], "dislikedSongs": [],
                 "bannedArtists": [], "artistDislikeCounts": {}},
     "events": []
   }
   ```
   Лимиты: ratings до 20000, events до 5000 за запрос — слать страницами.
   Ответ вернёт `fav_added / dis_added / profile_banned / auto_bans`.

2. **Аппендиксы в процессе слушания** (каждые N событий или при запросе волны):
   - `POST /api/users/{id}/events` — `{"events": [{"track_id": "<ext или uuid>",
     "action": "play|complete|skip|replay|seek_back|abandon",
     "position_sec": 12}]}`. Сервер применит правила: 3 скипа → автодизлайк,
     3 дизлайка артиста → автобан (вернёт `auto_dislikes / auto_bans` — показать тост).
   - `POST /api/users/{id}/rate` — `{"track_id": "...", "like": true|false|null}`.
   - `POST /api/users/{id}/history` — `{"track_id": "...", "played_at": "ISO"}` —
     факт воспроизведения в память мозга (кормит волну и историю).
   - Либо всё разом через `POST /api/wave/continue` (см. ниже) — дельта применяется сама.

3. **Динамическая волна** (очередь живёт в клиенте, мозг только докладывает):
   `POST /api/wave/continue`
   ```json
   {
     "user_id": "...", "queue": ["<uuid или ext, уже в очереди>"],
     "current_track_id": "...", "count": 20,
     "settings": {"activity": "work|workout|sleep",
                  "characteristic": "favorite|unfamiliar|popular",
                  "mood": "chill", "language": "ru"},
     "exclude_ids": [], "recent_events": [], "ratings_delta": []
   }
   ```
   Ответ: `tracks[{track_id, title, artist_name, score, reason}] + seeds + applied
   + profile_version`. Клиент append'ит к очереди. `GET /api/wave/seeds` — сиды отдельно.

4. **Профиль/история для экранов** (опционально):
   - `GET /api/users/{id}/profile` — веса жанров/артистов, топ треков со скором,
     часы/дни, настроения, баны (готовая модель для экрана «Вкусы»).
   - `GET /api/users/{id}/history?limit&offset`, `GET /api/users/{id}/recent-events`.
   - `GET /api/collab/similar-users/{id}`, `GET /api/collab/recommend/{id}?n=`.

5. **Vault (автообновление вкусов ночью, opt-in)**:
   - `POST /api/users/{id}/vault {"password": "..."}` — запомнить пароль Navidrome
     (Fernet-шифр, ключ `TASTE_VAULT_KEY` только в compose). Требует включённого тумблера
     «автообновление» + согласия пользователя (пароль — чувствительные данные).
   - `GET .../vault` — статус, `DELETE .../vault` — забыть,
     `POST .../refresh-now` — обновить сейчас без ожидания ночи.

## Правила поведения клиента

- Всё общение — best-effort: сервер недоступен → тихий fallback на локальную логику,
  очередь не должна рваться. Ретраи с backoff, без спама.
- Идемпотентность: повторный `sync-from-mobile` безопасен (счётчики — max-семантика).
- `like=false` на сервере может вернуть `auto_banned_artist` — показать пользователю.
- Настройки волны (`activity/characteristic/mood`) слать как есть из `MyWaveSettings`.
- Не слать пароль никуда кроме `vault` / `by-credentials` / `import-tastes`, по HTTP
  только в доверенной сети (см. про auth выше).

## Что НЕ нужно клиенту

- Экспорт волны в Navidrome — волны там нет, это только наш клиент.
- Cron-автоволна на сервере — волна динамическая, решает клиент каждым запросом.
- Дедуп (`/api/library/duplicates`) и sonic-анализ — внутренние дела мозга/веба.

## Статус мозга (готово, покрыто smoke-тестами)

taste engine + sync ⇄, collab + compare, dedup, vault + `refresh_tastes`,
`my-wave` плейлистом, `wave/continue` + seeds, history/recent-events,
user profile, genre clouds + сравнение. OpenAPI: `/api/docs`.
