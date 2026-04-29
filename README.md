# MAX Comments WebApp

Бот для MAX, который открывает отдельное мини-приложение с комментариями для каждого поста в канале.

Теперь сценарий такой:

- администратор публикует пост напрямую в канале или через бота
- бот автоматически добавляет под пост кнопку `Комментарии`
- кнопка открывает мини-приложение MAX по диплинку `?startapp=...`
- мини-приложение загружает только один конкретный пост и только его комментарии
- мини-приложение показывает ленту в формате чата, автоматически подтягивает новые комментарии, умеет подгружать ранние сообщения, отвечать на комментарии, прикладывать фото, позволяет пользователю удалить или отредактировать свой комментарий и для админа даёт удаление любых комментариев
- комментарии сохраняются в SQLite и дублируются в отдельный чат обсуждения

## Что есть в проекте

- [bot.py](/Users/urij/vscode/bot.py:1) — бот MAX, API-клиент, SQLite-слой и встроенный HTTP-сервер
- [webapp/index.html](/Users/urij/vscode/webapp/index.html:1) — страница мини-приложения
- [webapp/app.js](/Users/urij/vscode/webapp/app.js:1) — загрузка поста, списка комментариев, фото и отправка нового комментария
- [webapp/app.css](/Users/urij/vscode/webapp/app.css:1) — оформление мини-приложения

## Команды

- `/publish текст поста` — публикует новый пост в канал через бота и вешает кнопку открытия WebApp
- `/attach MESSAGE_ID` — подключает WebApp к уже существующему посту
- `/posts` — показывает последние зарегистрированные посты
- `/me` — показывает `user_id`
- `/chatinfo` — показывает `chat_id` текущего чата

Если администратор пишет пост вручную прямо в настроенный канал, бот тоже автоматически зарегистрирует его и прикрепит кнопку комментариев.

## Требования

- Python 3.9+
- бот MAX с токеном
- бот должен быть администратором канала и чата обсуждения
- мини-приложение должно быть подключено к этому же боту в кабинете MAX
- URL мини-приложения в MAX должен быть публичным и работать по `https`

## Настройка

Обязательные переменные:

```bash
export MAX_BOT_TOKEN="..."
export MAX_CHANNEL_CHAT_ID="-123456789"
export MAX_COMMENTS_CHAT_ID="-987654321"
export MAX_COMMENTS_CHAT_URL="https://max.ru/..."
export MAX_ADMIN_USER_IDS="11111111,22222222"
```

Дополнительно для WebApp и встроенного сервера:

```bash
export MAX_WEB_SERVER_ENABLED="1"
export MAX_WEB_SERVER_HOST="127.0.0.1"
export MAX_WEB_SERVER_PORT="8080"
export MAX_WEB_APP_PUBLIC_URL="https://your-domain.example"
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

## GitHub CI/CD

В проект уже добавлены:

- `.github/workflows/ci-cd.yml` — GitHub Actions workflow
- `scripts/check.sh` — быстрая проверка синтаксиса Python
- `scripts/deploy_vps.sh` — выкладка `bot.py`, `config.py` и `webapp/*` на VPS

Как работает pipeline:

1. На `pull_request` и `push` запускается проверка `scripts/check.sh`.
2. На `push` в `main` и при ручном `workflow_dispatch` после проверки идёт деплой на сервер.
3. Workflow копирует файлы на VPS, запускает `python3 -m py_compile bot.py config.py`, затем перезапускает `systemd`-сервис.

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

1. Администратор публикует пост прямо в канале или отправляет боту `/publish Текст поста`.
2. Бот получает событие нового поста или подхватывает его фоновым сканером канала, регистрирует его и создаёт техническую тему обсуждения в отдельном чате комментариев.
3. Бот редактирует пост и добавляет кнопку `Комментарии`.
4. Кнопка открывает ссылку вида `https://max.ru/<botName>?startapp=post_<token>`.
5. MAX открывает мини-приложение, а `window.WebApp.initDataUnsafe.start_param` содержит ссылку на конкретный пост.
6. Мини-приложение запрашивает у бэкенда только этот пост и только его комментарии.
7. При отправке комментария мини-приложение передаёт `window.WebApp.initData`.
8. Бэкенд валидирует подпись `initData`, сохраняет комментарий и отправляет его в чат обсуждения.

## API мини-приложения

- `GET /api/healthz`
- `GET /api/posts/<post_ref>`
- `GET /api/posts/<post_ref>/comments`
- `POST /api/posts/<post_ref>/comments`
- `PATCH /api/posts/<post_ref>/comments/<comment_id>`
- `DELETE /api/posts/<post_ref>/comments/<comment_id>`

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

Для удаления комментария WebApp отправляет `DELETE` с заголовком `X-Max-Init-Data`; удаление доступно только пользователям из `MAX_ADMIN_USER_IDS`.

## Как получить ID

- Откройте личный чат с ботом и отправьте `/me`, чтобы узнать `MAX_ADMIN_USER_IDS`
- Отправьте `/chatinfo` в канале и в чате обсуждения, чтобы узнать `chat_id`

## Важные замечания

- Для production документация MAX рекомендует Webhook, но в этом проекте для простоты оставлен Long Polling.
- Встроенный HTTP-сервер подходит для разработки и небольших инсталляций. Для production лучше поставить его за обратным прокси.
- Проверка `initData` для WebApp реализована на стороне Python по официальному алгоритму HMAC-SHA256.
- Если `MAX_CHANNEL_CHAT_ID` и `MAX_COMMENTS_CHAT_ID` совпадают, посты и техническая лента обсуждения будут смешаны в одном месте.
- Если MAX не присылает событие о ручной публикации поста в канале, бот всё равно подцепит такой пост фоновым сканером за несколько секунд.

## Официальная документация MAX

- Общая документация: https://dev.max.ru/docs
- API: https://dev.max.ru/docs-api
- Подключение мини-приложения: https://dev.max.ru/docs/webapps/introduction
- MAX Bridge: https://dev.max.ru/docs/webapps/bridge
- Валидация `initData`: https://dev.max.ru/docs/webapps/validation
- Отправка сообщений: https://dev.max.ru/docs-api/methods/POST/messages
