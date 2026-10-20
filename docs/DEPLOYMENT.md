# Деплой MRC Rooms

## 1. Репозиторий GitHub

Создайте пустой репозиторий `mrc-rooms` в своём аккаунте. В локальном проекте:

```sh
git add .
git commit -m "Prepare MRC Rooms for deployment"
git remote add origin git@github.com:YOUR_USERNAME/mrc-rooms.git
git push -u origin main
```

Git должен знать ваше имя и email для коммита. Используйте свои значения,
при необходимости настройте `git config user.name` и `git config user.email`.
В `.gitignore` исключены ключи, `.env`, SQLite, переписки, резервные копии и логи.
Docker build использует список разрешённых файлов в `.dockerignore`.

После push вкладка **Actions** покажет проверки Python, браузеров и Docker.
Образ и приложение автоматически не публикуются.

## 2. Сервер и домен

Нужны Linux-сервер с Docker Engine / Compose и домен, DNS которого вы можете менять.
Внешний IP и домен могут быть бесплатными, если ваш провайдер это предоставляет;
репозиторий сам по себе не предоставляет вычисления или постоянный диск.

1. Направьте DNS-запись `A` домена на публичный IPv4 сервера.
2. Добавляйте `AAAA` только при рабочем IPv6 до этого сервера.
3. Разрешите входящие TCP-порты **80 и 443**. UDP 443 необязателен, нужен для HTTP/3.
4. Установите Docker Engine и плагин Compose по инструкции для вашей ОС.
5. Клонируйте репозиторий и войдите в его каталог.

```sh
git clone git@github.com:YOUR_USERNAME/mrc-rooms.git
cd mrc-rooms
cp .env.example .env
chmod 600 .env
python3 -c 'import secrets; print(secrets.token_hex(32))'
```

Вставьте сгенерированное значение в `SECRET_KEY` файла `.env`.
Укажите `DOMAIN`, например `rooms.your-domain.com`, без `https://` и путей.
Сохраните секрет в надёжном месте и не меняйте при обновлениях.
Не используйте тестовый ключ из CI.

```sh
docker compose -f compose.yaml -f compose.production.yaml config --quiet
docker compose -f compose.yaml -f compose.production.yaml up -d --build
docker compose -f compose.yaml -f compose.production.yaml ps
curl --fail https://rooms.your-domain.com/health
```

Caddy получит TLS-сертификат и перенаправит HTTP на HTTPS. Сертификаты и база
хранятся в именованных volumes. Порт приложения 8010 привязан только к localhost.
В production отсутствие постоянного `SECRET_KEY` останавливает запуск, Secure
cookie и HSTS включаются автоматически.

## 3. Постоянное хранилище

Контейнер приложения работает от UID/GID **10001** и пишет только в `/data`.
Compose создаёт volume `rooms-data`, который получает права из образа.
При использовании bind mount вместо именованного volume каталог на сервере
должен быть доступен для записи UID 10001.

Для PaaS с поддержкой Docker:

- собирайте образ из `Dockerfile`;
- задайте `MRC_ENV=production` и постоянный `SECRET_KEY`;
- подключите постоянный диск в `/data`;
- настройте HTTP health check `/health`;
- направьте HTTPS-трафик платформы на `PORT` (по умолчанию 8010);
- запускайте **одну реплику**, без автоматического горизонтального масштабирования.

На бесплатных тарифах постоянный диск часто недоступен. Без него комнаты и чат
пропадут при пересоздании контейнера. Не размещайте SQLite на общем NFS-диске.

## 4. TURN

Для голоса между разными сетями настройте свой TURN или TURN-провайдера через
`ICE_SERVERS_JSON` в `.env` (пример в README). HTTPS-туннель или reverse proxy
сам по себе не заменяет TURN.

После изменения настройки:

```sh
docker compose -f compose.yaml -f compose.production.yaml up -d
```

Участники должны обновить страницы и подключить голос заново. Для диагностики:

```sh
docker compose logs --tail 100 app
```

События `voice_diagnostic` содержат состояния ICE и типы кандидатов:
`host` — локальный, `srflx` — внешний адрес через STUN, `relay` — ретрансляция TURN.
`ice=failed` означает, что браузеры не нашли работающий путь.
Отсутствие `relay` при ненастроенном TURN ожидаемо. Ошибка ICE 701 может означать
недоступность STUN/TURN, но одна такая запись не доказывает полный отказ:
проверяйте итоговое состояние `connected` / `failed`.

## 5. Обновление

Сначала создайте резервную копию (ниже), затем:

```sh
git pull --ff-only
docker compose -f compose.yaml -f compose.production.yaml up -d --build
curl --fail https://rooms.your-domain.com/health
```

Не используйте `docker compose down -v`: флаг `-v` удаляет данные.
Обычный `down` или пересборка контейнера сохраняют именованные volumes.
Перезапуск прерывает присутствие участников; им может понадобиться войти в комнату
и подключить голос заново.

## 6. Резервная копия и восстановление

SQLite использует WAL: нельзя просто скопировать открытый файл базы без WAL.
Для простого согласованного бэкапа остановите приложение:

```sh
backup_dir="backups/$(date +%Y%m%d-%H%M%S)"
mkdir -p "$backup_dir"
chmod 700 "$backup_dir"
docker compose -f compose.yaml -f compose.production.yaml stop app
docker compose cp app:/data/. "$backup_dir/"
docker compose -f compose.yaml -f compose.production.yaml start app
```

Также сохраните `.env` с `SECRET_KEY` в безопасном месте отдельно от репозитория.
Держите копию вне сервера.

Для восстановления остановите приложение и скопируйте содержимое выбранного
бэкапа в `/data` остановленного контейнера:

```sh
docker compose -f compose.yaml -f compose.production.yaml stop app
docker compose cp backups/YOUR_BACKUP/. app:/data/
docker compose run --rm --no-deps --user root app chown -R 10001:10001 /data
docker compose -f compose.yaml -f compose.production.yaml start app
```

Восстанавливайте весь каталог данных из одной копии. Сначала сохраните текущие
данные отдельно. Верните прежний `SECRET_KEY`, чтобы сохранить действующие сессии.

## Временная публичная демонстрация

Если нужен быстрый показ проекта без отдельного сервера, локальный waitress
можно открыть через бесплатный HTTPS-туннель:

```sh
# Первый терминал:
COOKIE_SECURE=1 .venv/bin/python app.py

# Второй терминал:
ssh -T -o ServerAliveInterval=30 -o ExitOnForwardFailure=yes \
  -o IdentitiesOnly=yes -o IdentityFile=none \
  -R 80:127.0.0.1:8010 nokey@localhost.run
```

Текущий адрес `https://….lhr.life` появится в терминале. Сервис может менять его
даже без перезапуска SSH. Mac, сервер и SSH должны оставаться включёнными.
Это временная демонстрация, не постоянный хостинг.
