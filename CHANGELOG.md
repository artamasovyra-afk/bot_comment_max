# Changelog

All notable changes to this project will be tracked here.

## [Unreleased]

### Added

- Переключаемый режим доставки событий `polling/webhook` с webhook-endpoint внутри встроенного HTTP-сервера.
- Автоматическое создание и обновление webhook-подписки MAX через `/subscriptions`.
- Браузерная админка `/admin` для управления каналами, публикациями, привязкой постов и синхронизацией.
- Команда `/setup_channel`, которая создаёт канал в админке и запускает мастер подключения чата комментариев.

### Changed

- `GET /api/healthz` теперь возвращает `delivery_mode`, а сервис читает рабочую версию из файла `VERSION`.
- Продовый `.env` можно перевести на webhook без отдельного внешнего обработчика.

### Fixed

- ...

## [0.1.0] - 2026-04-30

### Added

- MAX bot and WebApp comments flow for channel posts with per-post discussion threads.
- Automatic comment button attachment for new channel posts and support for multiple connected channels.
- WebApp chat interface with replies, editing, deletion, notifications, photo attachments, and link blocking.
- GitHub Actions CI/CD deployment to the VPS.
