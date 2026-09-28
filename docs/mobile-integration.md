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
     "action": "play|complete|skip|replay|seek_back|abandon|like|dislike",
     "position_sec": 12}]}`. Сервер применит правила: 3 скипа → автодизлайк,
     3 дизлайка артиста → автобан (вернёт `auto_dislikes / auto_bans` — показать тост).
     `like`/`dislike` в events работают как rate: создают Favorite/Dislike и
     закрывают «показ» в метриках волны (раньше такие события молча дропались —
     был «ЛАЙК С ВОЛНЫ 0%» при живых лайках).
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

3a. **Живая очередь на веб (`/wave`)**: очередь живёт НА КЛИЕНТЕ (как у
   Яндекс Музыки), мозг её только показывает и анализирует. Клиент публикует
   её при каждом изменении:
   `POST /api/wave/publish {"user_id": "...", "queue": ["<ext...>"], "current_track_id": "...", "device": "<stable-id>"}`
   (fire-and-forget, best-effort, throttle ~5с). Веб тянет `GET /api/wave/live?user_id=...`
   (очередь + current + age_sec, TTL 10 мин). Хранилище переживать
   рестарт не обязано — клиент перепубликует при следующем изменении очереди.

   **device — ОБЯЗАТЕЛЬНО стабильный**: сгенерируй UUID один раз при первом
   запуске, сохрани в настройках, шли всегда один и тот же. Каждый device —
   ОТДЕЛЬНЫЙ слот: телефон и десктоп друг друга не затирают (раньше был один
   слот на юзера — last-writer-wins, и веб видел кашу «Сейчас #3 vs Продолжить
   #1»). Без device — слот `default` (старые клиенты). `GET /live` без `?device`
   отдаёт самый свежий слот + `devices[]` (все живые плееры); с `?device=<id>` —
   конкретный плеер. Кнопка «продолжить» на другом устройстве = взять очередь
   и `current_track_id` + `position_sec` из его слота, открыть трек в своём
   плеере и перемотать.

    **Перенос прослушивания между устройствами** (начал на телефоне - продолжил
    на десктопе). Мозг не стримит звук, он только помнит где остановились.
    Чтобы это работало, клиент добавляет в тот же publish:

    ```json
    {"position_sec": 83, "duration_sec": 214, "device": "pixel", "paused": false}
    ```

    Другое устройство забирает `GET /api/wave/resume?user_id=...` и получает
    `{track, position_sec, position_ratio, queue[], age_sec, stale}`. Клиент сам
    открывает трек в своем плеере и перематывает на position_sec. Если
    `stale: true` - очередь могла измениться, продолжать осторожно. Позицию не
    нужно слать на каждый тик: достаточно на паузе, смене трека и раз в ~15 с
    (ключ живет 10 минут и перезаписывается целиком).

3a2. **Общие настройки волны (одни на всех устройствах)**. Пилюли, выбранные
    на любом плеере, хранятся на мозге:
    `GET /api/wave/settings?user_id=...` → `{settings{mood?, activity?, characteristic?, language?}, version, updated_at}`
    (читать при старте плеера), `PUT /api/wave/settings {"user_id", "settings"}`
    (писать при смене пилюль; пустая строка стирает пилюлю, `PUT {"settings": {}}`
    или `DELETE /api/wave/settings?user_id=` — сброс для всех).
    `POST /api/wave/continue`: чего не прислали в `settings` — мозг доберёт из
    общих; явный ключ (хоть пустой = «авто») побеждает только на этот запрос.
    Сценарии: настроил волну на мобильном → ПК при запуске прочитал и играет то
    же; сбросил на одном → у всех «авто», очередь перестроится следующей докруткой.
    Сценарий «послушал на мобильном → дома нажал Продолжить на ПК»: ПК читает
    `GET /api/wave/resume?user_id=` (без `?device` — самый свежий слот),
    открывает трек в своём плеере и мотает на `position_sec`, очередь — из `queue[]`.

3b. **Server-driven очередь (мозг отдаёт очередь в AutoDJ)**. Отдельного
   endpoint не нужно — очередь отдаёт `POST /api/wave/continue` (треки уже идут
   с `external_id` = Navidrome song id, т.е. сразу играбельны). Клиентский цикл:
   - триггер refill: в очереди осталось ≤5 треков или сменился current —
     `waveContinue{queue, current_track_id, count: 10-20, settings, exclude_ids}`;
   - `exclude_ids`: сюда же складывать id, которых нет в локальной библиотеке
     (missing из резолвера), — сервер их больше не предлагает;
   - фидбек — как обычно: `reportEvent play/skip/complete` + `reportRate`,
     отдельными вызовами (внутрь `waveContinue` дублировать не надо);
   - при старте волны с нуля: `queue: [], current_track_id: <seed или null>`.
   Важно: встраивать как **источник внутри SmartAutoDJ** (рядом с локальным
   скорингом, с фолбеком на него при недоступности мозга), а не отдельным
   плеером/модулем — очередь, управление и эквалайзер общие.

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
