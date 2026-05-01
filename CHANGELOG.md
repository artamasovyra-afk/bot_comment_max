# Changelog

All notable changes to this project will be tracked here.

## [Unreleased]

## [0.1.2] - 2026-05-01

### Added

- Разделение ролей админки `super_admin` и `channel_admin`.
- Вход в админку по логину/паролю вместо общего `MAX_ADMIN_PANEL_TOKEN`.
- Создание первого `super_admin` через `MAX_SUPER_ADMIN_IDS`.
- Cookie-сессии админки и смена пароля с хранением пароля только в виде хеша.
- Управление администраторами каналов из раздела `Администраторы`.
- Backend-проверки прав для админских API с ограничением `channel_admin` только своими каналами.

### Changed

- `MAX_ADMIN_PANEL_TOKEN` помечен как устаревший и больше не используется для входа в новую ролевую админку.
- Документация обновлена под роли, логин/пароль и создание первого супер-администратора.

## [0.1.1] - 2026-05-01

### Added

- Переключаемый режим доставки событий `polling/webhook` с webhook-endpoint внутри встроенного HTTP-сервера.
- Автоматическое создание и обновление webhook-подписки MAX через `/subscriptions`.
- Браузерная админка `/admin` для управления каналами, публикациями, привязкой постов и синхронизацией.
- Команда `/setup_channel`, которая создаёт канал в админке и запускает мастер подключения чата комментариев.

### Changed

- `GET /api/healthz` теперь возвращает `delivery_mode`, а сервис читает рабочую версию из файла `VERSION`.
- Продовый `.env` можно перевести на webhook без отдельного внешнего обработчика.

## [0.1.0] - 2026-04-30

### Added

- MAX bot and WebApp comments flow for channel posts with per-post discussion threads.
- Automatic comment button attachment for new channel posts and support for multiple connected channels.
- WebApp chat interface with replies, editing, deletion, notifications, photo attachments, and link blocking.
- GitHub Actions CI/CD deployment to the VPS.
