!!!Alert!!! Вайбкодинг)
# MAX Comments WebApp

Бот для MAX, который открывает отдельное мини-приложение с комментариями для каждого поста в канале.

Теперь сценарий такой:

- администратор публикует пост напрямую в канале или через бота
- бот автоматически добавляет под пост кнопку с количеством комментариев, например `0 комментариев`
- кнопка открывает мини-приложение MAX по диплинку `?startapp=...`
- мини-приложение загружает только один конкретный пост и только его комментарии
- мини-приложение показывает ленту в формате чата, автоматически подтягивает новые комментарии, умеет подгружать ранние сообщения, отвечать на комментарии, прикладывать фото, позволяет пользователю удалить или отредактировать свой комментарий и даёт администраторам модерировать комментарии только в доступных им каналах
- комментарии сохраняются в SQLite и дублируются в отдельный чат обсуждения

## Что есть в проекте

- [bot.py](/Users/urij/vscode/bot.py:1) — бот MAX, API-клиент, SQLite-слой и встроенный HTTP-сервер
- [webapp/index.html](/Users/urij/vscode/webapp/index.html:1) — страница мини-приложения
- [webapp/app.js](/Users/urij/vscode/webapp/app.js:1) — загрузка поста, списка комментариев, фото и отправка нового комментария
- [webapp/app.css](/Users/urij/vscode/webapp/app.css:1) — оформление мини-приложения

## Команды

- `/publish текст поста` — публикует новый пост в канал через бота и вешает кнопку открытия WebApp
- `/publish CHANNEL_ID текст поста` — публикует пост в выбранный подключённый канал
- `/attach MESSAGE_ID` — подключает WebApp к уже существующему посту
- `/channels` — показывает подключённые каналы
- `/channel_add CHANNEL_ID COMMENTS_CHAT_ID` — подключает канал к чату комментариев
- `/channel_remove CHANNEL_ID` — отключает канал
- `/setup_channel` — отправляется прямо в канале, создаёт канал в админке и запускает мастер подключения
- `/bind_channel` — отправляется прямо в канале, подготавливает автопривязку без ручного ввода ID
- `/bind_comments CODE` — отправляется в чате комментариев и завершает привязку
- `/bind_status` — показывает активные незавершённые привязки и время до истечения кода
- `/posts` — показывает последние зарегистрированные посты
- `/me` — показывает `user_id`
- `/chatinfo` — показывает `chat_id` текущего чата

Если администратор пишет пост вручную прямо в подключённый канал, бот тоже автоматически зарегистрирует его и прикрепит кнопку комментариев.

## Требования

- Python 3.9+
- бот MAX с токеном
- бот должен быть администратором каждого подключаемого канала и каждого чата обсуждения
- мини-приложение должно быть подключено к этому же боту в кабинете MAX
- URL мини-приложения в MAX должен быть публичным и работать по `https`

## Настройка

Обязательные переменные:

```bash
export MAX_BOT_TOKEN="..."
export MAX_SUPER_ADMIN_IDS="11111111,22222222"
export ADMIN_SESSION_SECRET="long-random-session-secret"
```

`MAX_ADMIN_USER_IDS` остаётся legacy-настройкой: если `MAX_SUPER_ADMIN_IDS` не задан, она используется для первичного создания `super_admin`.
`MAX_ADMIN_PANEL_TOKEN` устарел и не используется для входа в новую ролевую админку.

Переменные ниже остаются как legacy-bootstrap и могут автоматически создать первую привязку канала при пустой базе:

```bash
export MAX_CHANNEL_CHAT_ID="-123456789"
export MAX_COMMENTS_CHAT_ID="-987654321"
export MAX_COMMENTS_CHAT_URL="https://max.ru/..."
```

Дополнительно для WebApp и встроенного сервера:

```bash
export MAX_WEB_SERVER_ENABLED="1"
export MAX_WEB_SERVER_HOST="127.0.0.1"
export MAX_WEB_SERVER_PORT="8080"
export MAX_WEB_APP_PUBLIC_URL="https://your-domain.example"
export MAX_DELIVERY_MODE="polling"
export MAX_WEBHOOK_PATH="/webhook"
export MAX_WEBHOOK_PUBLIC_URL="https://your-domain.example/webhook"
export MAX_WEBHOOK_SECRET="change-me"
export MAX_WEB_APP_AUTH_MAX_AGE_SECONDS="3600"
export MAX_CHANNEL_SYNC_INTERVAL_SECONDS="5"
```

Прочие необязательные настройки:

```bash
export MAX_DATABASE_PATH="/absolute/path/to/max_comments.sqlite3"
export MAX_POLL_TIMEOUT_SECONDS="30"
export MAX_POLL_LIMIT="100"
```

## Запуск

Одна команда поднимает и бота, и встроенный HTTP-сервер:

```bash
python3 bot.py
```

Локально сервер мини-приложения откроется на:

```text
http://127.0.0.1:8080/
```

Для реального открытия внутри MAX нужен публичный `https`-адрес. Обычно схема такая:

1. Запускаете этот процесс на сервере или VM.
2. Проксируете `MAX_WEB_SERVER_PORT` через Nginx/Caddy.
3. Получаете публичный `https://your-domain.example`.
4. Указываете этот URL в настройках мини-приложения MAX.
5. Записываете тот же адрес в `MAX_WEB_APP_PUBLIC_URL`.

## Режим доставки событий

Бот умеет работать в двух режимах:

- `MAX_DELIVERY_MODE=polling` — удобно для локальной разработки и диагностики
- `MAX_DELIVERY_MODE=webhook` — рекомендуемый production-режим

Для `webhook` нужно:

- публичный `https`-домен
- reverse proxy на `443`
- `MAX_WEBHOOK_PUBLIC_URL` или `MAX_WEB_APP_PUBLIC_URL`
- желательно `MAX_WEBHOOK_SECRET`

Если `MAX_WEBHOOK_PUBLIC_URL` не задан, бот автоматически соберёт его как:

```text
MAX_WEB_APP_PUBLIC_URL + MAX_WEBHOOK_PATH
```

При старте в режиме `webhook` бот сам:

- проверяет локальную готовность endpoint
- создаёт или обновляет подписку через `POST /subscriptions`
- удаляет устаревшие webhook-подписки с других URL

При старте в режиме `polling` бот пытается отключить webhook на своём текущем URL, чтобы long polling снова работал.

## Админка и роли

Браузерная админка доступна на:

```text
https://your-domain.example/admin
```

Вход выполняется по логину и паролю:

- логин — MAX user id администратора
- пароль по умолчанию — тот же MAX user id
- после входа с паролем по умолчанию админка показывает предупреждение и форму смены пароля
- сессия хранится в HttpOnly cookie, пароль в браузере не сохраняется

Первый `super_admin` создаётся автоматически при старте из:

```bash
export MAX_SUPER_ADMIN_IDS="123456789"
```

Если переменная не задана, проект для обратной совместимости возьмёт значения из `MAX_ADMIN_USER_IDS`.

Роли:

| Возможность | super_admin | channel_admin |
|---|---|---|
| Видеть все каналы | Да | Нет |
| Управлять своими каналами | Да | Да |
| Публиковать посты | Да | Только свои каналы |
| Смотреть комментарии | Да | Только свои каналы |
| Обрабатывать жалобы | Да | Только свои каналы |
| Назначать администраторов | Да | Нет |
| Настройки webhook/polling | Да | Нет |
| Глобальные настройки бота | Да | Нет |

Через админку можно:

- смотреть состояние бота, версию и режим доставки событий
- добавлять и удалять привязки каналов, если вошёл `super_admin`
- назначать `channel_admin` на один или несколько каналов, если вошёл `super_admin`
- публиковать посты в доступные каналы
- прикреплять комментарии к уже существующим постам
- запускать ручную синхронизацию последних постов, если вошёл `super_admin`
- смотреть посты, комментарии и жалобы с backend-проверкой прав

Команды в MAX остаются как запасной способ управления.

Чтобы назначить администратора канала:

1. Войдите в `/admin` как `super_admin`.
2. Откройте раздел `Администраторы`.
3. Укажите MAX user id, роль `channel_admin` и выберите доступные каналы.
4. Сохраните. Новый администратор входит с логином MAX user id и паролем по умолчанию MAX user id.

Чтобы сменить пароль, войдите в `/admin` и используйте форму в предупреждении `Вы используете пароль по умолчанию`.
Выход выполняется кнопкой `Выйти`, backend очищает cookie-сессию.

## Инструкция для администратора канала

Так подключается новый канал без ручного поиска `chat_id`.

1. Добавьте бота в канал.
2. Дайте боту права администратора канала.
3. Создайте или откройте отдельный чат для комментариев.
4. Добавьте бота в этот чат и тоже дайте ему права администратора.
5. В самом канале отправьте:

```text
/setup_channel
```

6. Бот создаст канал в админке со статусом `Ожидает чат комментариев` и пришлёт код привязки.
7. Скопируйте команду из сообщения бота или нажмите готовую кнопку выбора чата. Команда выглядит так:

```text
/bind_comments CODE
```

8. Отправьте эту команду в чате комментариев.
9. Дождитесь сообщения `Привязка завершена`.
10. После этого новые посты в канале будут автоматически получать кнопку `0 комментариев`, `1 комментарий`, `2 комментария` и так далее.

Если комментарии должны жить в том же самом чате, можно сразу в канале использовать:

```text
/setup_channel same
```

Если команда не сработала, проверьте, что бот добавлен администратором и в канал, и в чат комментариев.

## Подключение нескольких каналов

После запуска можно подключать сколько угодно каналов к одному и тому же боту. Для каждого канала повторите инструкцию выше и укажите свой чат комментариев.

Проверить, какие коды привязки ещё активны:

```text
/bind_status
```

Базовый сценарий:

1. Добавьте бота в канал и в чат обсуждения.
2. Дайте боту права администратора.
3. Получите `chat_id` канала и чата через `/chatinfo`.
4. В личке с ботом выполните:

```text
/channel_add CHANNEL_ID COMMENTS_CHAT_ID
```

Посмотреть список подключённых каналов:

```text
/channels
```

Отключить канал:

```text
/channel_remove CHANNEL_ID
```

Если подключён только один канал, старая команда публикации остаётся короткой:

```text
/publish Текст поста
```

Если каналов несколько, используйте:

```text
/publish CHANNEL_ID Текст поста
```

## GitHub CI/CD

В проект уже добавлены:

- `.github/workflows/ci-cd.yml` — GitHub Actions workflow
- `.github/workflows/release.yml` — GitHub Release workflow по тегам `v*`
- `scripts/check.sh` — быстрая проверка синтаксиса Python
- `scripts/deploy_vps.sh` — выкладка `bot.py`, `config.py` и `webapp/*` на VPS
- `scripts/release.sh` — локальный помощник для создания release-тега

Как работает pipeline:

1. На `pull_request` и `push` запускается проверка `scripts/check.sh`.
2. На `push` в `main` и при ручном `workflow_dispatch` после проверки идёт деплой на сервер.
3. Workflow копирует файлы на VPS, запускает `python3 -m py_compile bot.py config.py`, затем перезапускает `systemd`-сервис.
4. На `push` тега вида `v0.1.0` GitHub автоматически создаёт Release.

## Версионирование

В репозитории есть единый файл версии:

```text
VERSION
```

Формат версии:

```text
MAJOR.MINOR.PATCH
```

История изменений ведётся в:

```text
CHANGELOG.md
```

Базовый release-flow:

1. Обновите `VERSION`.
2. Добавьте запись для этой версии в `CHANGELOG.md`.
3. Закоммитьте изменения в `main`.
4. Создайте тег:

```bash
scripts/release.sh
```

5. Отправьте тег на GitHub:

```bash
git push origin main v$(cat VERSION)
```

После этого GitHub Actions сам создаст Release для этого тега.

Перед первым запуском нужно:

1. Создать GitHub-репозиторий и запушить туда этот проект.
2. Добавить GitHub Actions Variables:
   - `DEPLOY_HOST=188.225.58.60`
   - `DEPLOY_PORT=22`
   - `DEPLOY_USER=root`
   - `DEPLOY_PATH=/opt/max-comments-webapp`
   - `DEPLOY_SERVICE=max-comments-webapp`
3. Добавить GitHub Actions Secret:
   - `DEPLOY_SSH_KEY`

Для `DEPLOY_SSH_KEY` нужен приватный SSH-ключ, а его публичную часть надо добавить на VPS в `authorized_keys`.

Пример генерации ключа локально:

```bash
ssh-keygen -t ed25519 -C "github-actions-max-comments" -f ~/.ssh/max-comments-github-actions
```

Пример добавления публичного ключа на сервер:

```bash
ssh root@188.225.58.60 'install -m 700 -d /root/.ssh && cat >> /root/.ssh/authorized_keys' < ~/.ssh/max-comments-github-actions.pub
```

После этого:

1. Содержимое `~/.ssh/max-comments-github-actions` сохраните в GitHub Secret `DEPLOY_SSH_KEY`.
2. Сделайте `push` в ветку `main` или запустите workflow вручную из вкладки `Actions`.

## Как это работает

1. Администратор публикует пост прямо в подключённом канале или отправляет боту `/publish ...`.
2. Бот получает событие нового поста или подхватывает его фоновым сканером канала, регистрирует его и создаёт техническую тему обсуждения в отдельном чате комментариев.
3. Бот редактирует пост и добавляет кнопку `Комментарии`.
4. Кнопка открывает ссылку вида `https://max.ru/<botName>?startapp=post_<token>`.
5. MAX открывает мини-приложение, а `window.WebApp.initDataUnsafe.start_param` содержит ссылку на конкретный пост.
6. Мини-приложение запрашивает у бэкенда только этот пост и только его комментарии.
7. При отправке комментария мини-приложение передаёт `window.WebApp.initData`.
8. Бэкенд валидирует подпись `initData`, сохраняет комментарий и отправляет его в чат обсуждения.

## API мини-приложения

- `GET /api/healthz` — возвращает `ok`, текущую `version` и `delivery_mode`
- `POST /api/admin/auth/login` — вход по MAX user id и паролю
- `POST /api/admin/auth/logout` — выход из админки
- `POST /api/admin/auth/change-password` — смена пароля
- `GET /api/admin/dashboard` — дашборд с учётом роли администратора
- `GET /api/admin/posts`
- `POST /api/admin/posts`
- `DELETE /api/admin/posts/<post_id>`
- `GET /api/admin/comments`
- `PATCH /api/admin/comments/<comment_id>/status`
- `GET /api/admin/reports`
- `PATCH /api/admin/reports/<report_id>`
- `GET /api/admin/users` — только `super_admin`
- `POST /api/admin/users` — только `super_admin`
- `PATCH /api/admin/users/<admin_user_id>` — только `super_admin`
- `POST /api/admin/users/<admin_user_id>/reset-password` — только `super_admin`
- `DELETE /api/admin/users/<admin_user_id>/channels/<channel_id>` — только `super_admin`
- `POST /api/admin/channels` — только `super_admin`
- `DELETE /api/admin/channels/<channel_id>` — только `super_admin`
- `POST /api/admin/publish`
- `POST /api/admin/attach` — только `super_admin`
- `POST /api/admin/sync` — только `super_admin`
- `GET /api/posts/<post_ref>`
- `GET /api/posts/<post_ref>/comments`
- `POST /api/posts/<post_ref>/comments`
- `PATCH /api/posts/<post_ref>/comments/<comment_id>`
- `DELETE /api/posts/<post_ref>/comments/<comment_id>`
- `POST /api/comments/<comment_id>/report`

Админские API проверяют права на backend: `super_admin` получает глобальный доступ, `channel_admin` — только к назначенным каналам. `channel_id` из frontend не считается источником истины.

Для `GET /api/posts/<post_ref>/comments` можно передавать:

- `limit`
- `before_id`

Для `POST` ожидается JSON:

```json
{
  "text": "Мой комментарий",
  "reply_to_comment_id": 42,
  "photo": {
    "data_url": "data:image/jpeg;base64,...",
    "file_name": "comment-photo.jpg"
  },
  "initData": "query_id=...&user=...&hash=..."
}
```

Для удаления комментария WebApp отправляет `DELETE` с заголовком `X-Max-Init-Data`; обычный пользователь может удалить только свой комментарий, администратор — только комментарии в каналах, к которым у него есть backend-доступ.

## Как получить ID

- Откройте личный чат с ботом и отправьте `/me`, чтобы узнать свой MAX user id для `MAX_SUPER_ADMIN_IDS` или назначения `channel_admin`
- Отправьте `/chatinfo` в канале и в чате обсуждения, чтобы узнать `chat_id`

## Важные замечания

- Для production документация MAX рекомендует Webhook; в проекте теперь поддерживаются оба режима через `MAX_DELIVERY_MODE`.
- Встроенный HTTP-сервер подходит для разработки и небольших инсталляций. Для production лучше поставить его за обратным прокси.
- Проверка `initData` для WebApp реализована на стороне Python по официальному алгоритму HMAC-SHA256.
- Если для какой-то привязки `CHANNEL_ID` и `COMMENTS_CHAT_ID` совпадают, посты и техническая лента обсуждения будут смешаны в одном месте.
- Если MAX не присылает событие о ручной публикации поста в канале, бот всё равно подцепит такой пост фоновым сканером за несколько секунд.

## Официальная документация MAX

- Общая документация: https://dev.max.ru/docs
- API: https://dev.max.ru/docs-api
- Подключение мини-приложения: https://dev.max.ru/docs/webapps/introduction
- MAX Bridge: https://dev.max.ru/docs/webapps/bridge
- Валидация `initData`: https://dev.max.ru/docs/webapps/validation
- Отправка сообщений: https://dev.max.ru/docs-api/methods/POST/messages
