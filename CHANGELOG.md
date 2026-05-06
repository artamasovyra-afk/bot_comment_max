# Changelog

All notable changes to this project will be tracked here.

## [Unreleased]

## [0.1.7] - 2026-05-06

### Added

- Новый сценарий подключения канала через заявку: админ принимает условия, добавляет бота администратором в канал и пересылает боту пост из канала.
- Таблицы `user_terms_acceptance` и `channel_connection_requests`.
- Раздел `Заявки` в `/super-admin` с одобрением и отклонением заявок.
- Super-admin API `/api/super-admin/channel-requests`.
- WebApp-only режим привязки канала без обязательного отдельного чата комментариев.

### Changed

- После одобрения заявки заявитель автоматически получает роль администратора канала.
- Автодобавление кнопки комментариев работает для новых постов подключённого канала без проверки автора поста в локальной админке.
- Legacy-команды `/setup_channel` и `/bind_comments CODE` сохранены как запасной способ подключения.

## [0.1.6] - 2026-05-01

### Fixed

- Повторное сохранение администратора канала теперь добавляет выбранные каналы к уже назначенным, а не заменяет весь список.

## [0.1.5] - 2026-05-01

### Fixed

- `/admin` теперь очищает неподходящую старую super-admin cookie и показывает форму входа администратора канала вместо экрана `Доступ запрещён`.

## [0.1.4] - 2026-05-01

### Fixed

- Исправлено падение формы добавления администратора канала в WebView/Safari, когда `event.currentTarget` становился `null` после асинхронного запроса.

## [0.1.3] - 2026-05-01

### Added

- Отдельная панель супер-администратора по адресу `/super-admin`.
- Отдельный вход супер-админа через `SUPER_ADMIN_LOGIN` и `SUPER_ADMIN_PASSWORD`.
- Отдельные super-admin API `/api/super-admin/*`.

### Changed

- `/admin` оставлен только для администраторов каналов с входом по MAX user id.
- Глобальные функции админки перенесены в панель супер-админа.
- Backend-проверки разделяют `/api/admin/*` и `/api/super-admin/*`.
- `MAX_ADMIN_PANEL_TOKEN` остаётся устаревшим и не используется для входа.
- Документация обновлена под две административные панели.

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
