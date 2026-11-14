# GraphQL Coverage Backend на Ubuntu VPS

Эта конфигурация запускает существующий FastAPI backend и PostgreSQL 16 Alpine
отдельным Compose-проектом `graphql-coverage`. Она предназначена для VPS Ubuntu
26.04.1 LTS с уже установленными Docker и Docker Compose. Команды ручного запуска ниже выполняет оператор на VPS. Отдельный безопасный
автодеплой через GitHub Actions описан в [AUTODEPLOY.md](AUTODEPLOY.md).

API публикуется только на `127.0.0.1:5081`, внутри контейнера слушает `8000`.
PostgreSQL доступен API как `db:5432` в собственной bridge-сети
`graphql-coverage_backend`; порт БД на хост не публикуется. Данные хранятся
в отдельном Docker volume `graphql-coverage_postgres-data`.
Не меняйте имя проекта через `-p` или `COMPOSE_PROJECT_NAME`: оно определяет
имена сети и volume. Сеть не подключается к проекту Vitrina.

С этой конфигурацией не заняты порты существующих Nginx (`80/443`), системного
PostgreSQL (`127.0.0.1:5432`), .NET (`5000/5100`), Redis (`6379`) и RustFS
(`9000/9001`). Порт `5081` был свободен при предварительной проверке; перед
запуском нужно проверить его снова. Если меняете `API_PORT`, выберите другой
свободный порт и скорректируйте команды `curl` ниже.

Frontend, demo GraphQL API и конфигурация Nginx в Compose не входят.
На VPS внешний Nginx принимает запросы к `https://graphql.rebyte.fun` и
передаёт разрешённые пути в API на loopback. Сам Docker по-прежнему не
публикует API напрямую в Интернет.

## 1. Размещение файлов

Работайте под выделенной учётной записью развёртывания с правом запуска Docker.
Не размещайте репозиторий в каталоге, который Nginx раздаёт как статические файлы.
Создайте отдельный каталог, доступный только владельцу и его группе:

```bash
sudo install -d -m 750 -o "$(id -un)" -g "$(id -gn)" /opt/graphql-coverage
git clone --branch main https://github.com/QualiMeter/GraphQL-Coverage-Proxy-Beckend.git /opt/graphql-coverage
cd /opt/graphql-coverage
```

Команда клонирования предполагает пустой каталог и доступ этой учётной записи
к репозиторию. После слияния PR файлы будут в `main`; для проверки PR до слияния
вместо `--branch main` укажите `--branch chore/vps-compose`.
Не используйте общий каталог или `.env` проекта Vitrina.

Во всех следующих командах рабочий каталог — `/opt/graphql-coverage`.
Используйте только `deploy/compose.production.yaml` с явным `--env-file`:
корневой `compose.yaml` предназначен для разработки и не должен запускаться
на этом VPS или объединяться с production-файлом.

## 2. Создание `.env`

При первом запуске создайте файл, не перезаписывая существующий `.env`:

```bash
(umask 077; cp -n deploy/.env.example deploy/.env)
chmod 600 deploy/.env
openssl rand -hex 32
${EDITOR:-nano} deploy/.env
```

Скопируйте результат `openssl rand -hex 32` в `POSTGRES_PASSWORD`. Это 64
шестнадцатеричных символа, безопасных для включения в URL без percent-encoding.
Пример намеренно содержит пустой пароль: с ним `docker compose config` и запуск
завершатся ошибкой. Не используйте пароль из dev-конфигурации.

`POSTGRES_PASSWORD` передаётся PostgreSQL как есть и включается в
`DATABASE_URL=postgresql+asyncpg://USER:PASSWORD@db:5432/DB` для API и Alembic.
Поэтому используйте сгенерированный hex-пароль, без `@`, `:`, `/`, `%`, `$`,
пробелов и других специальных символов. Не кодируйте пароль только для одной
из сторон. Для `POSTGRES_DB` и `POSTGRES_USER` оставьте значения примера или
используйте простые имена из букв, цифр и подчёркиваний.

Остальные переменные:

| Переменная | Назначение |
| --- | --- |
| `API_PORT` | Порт на loopback хоста; по умолчанию `5081` |
| `API_ROOT_PATH` | Публичный префикс, который Nginx убирает при проксировании; если не задан — `/coverage`, для отдельного домена `graphql.rebyte.fun` установить `API_ROOT_PATH=` (пустое значение) |
| `TARGET_GRAPHQL_URL` | Необязательный URL целевого GraphQL API; можно оставить пустым |
| `COVERAGE_API_KEY` | Секрет длиной от 32 символов для приватных REST-операций и `/graphql`; `openssl rand -hex 32`; хранить только в `deploy/.env` |
| `CORS_ORIGINS` | Разрешённые origins через запятую, например `https://coverage.example.com`; пустое значение отключает cross-origin доступ |

Текущий backend использует `TARGET_GRAPHQL_URL` для `/graphql` и импорта схемы
через introspection. Если URL не задан, `/graphql` вернёт 503, но REST/health
продолжат работать. При настройке URL учтите, что `127.0.0.1` внутри
API-контейнера указывает на сам контейнер.
Для CORS задавайте точные origins со схемой и при необходимости портом,
без пути и завершающего `/`; wildcard в production не используйте.

Для выделенного домена `https://graphql.rebyte.fun` установите в
`deploy/.env` **явно пустую** переменную `API_ROOT_PATH=`. Nginx должен
проксировать `/health`, `/docs`, `/openapi.json`, `/api/…` и `/graphql`
на `http://127.0.0.1:5081` с сохранением исходного пути.
У Swagger будет `/openapi.json`, а не прежний `/coverage/openapi.json`.
Старый путь `https://rebyte.fun/coverage/` не использовать для новых клиентов.
Публичное чтение ограничено `isPublic=true`, административные изменения,
журнал запросов и GraphQL-прокси защищены `COVERAGE_API_KEY`.

`.env` исключён из Git, `.env.example` остаётся в репозитории. Не публикуйте
`.env`, полный вывод `docker compose config` или `docker inspect`: они содержат
пароль. Доступ к Docker также даёт возможность прочитать окружение контейнеров.
Переменные shell имеют приоритет над `--env-file`; перед запуском убедитесь,
что в shell нет старых экспортированных значений этих переменных.

## 3. Проверка конфигурации

```bash
sudo ss -ltnp '( sport = :5081 )'
docker compose --env-file deploy/.env -f deploy/compose.production.yaml config --quiet
```

Если `ss` показывает слушающий сокет на этом порту, сначала разрешите конфликт
или выберите свободный `API_PORT`. Проверка `config --quiet` должна завершиться
с кодом `0` и без вывода. Она проверяет Compose-конфигурацию и обязательные
переменные, но не проверяет доступность порта, пароль существующей БД или сеть.

`build.context: ..` разрешается относительно каталога `deploy`, то есть
указывает на корень репозитория и использует существующий `Dockerfile`.
Секреты не передаются в build arguments. У PostgreSQL нет секции `ports`;
его внутренний порт `5432` не конфликтует с системным PostgreSQL.
Синтаксис переменных и разрешение build context описаны в документации
[Docker Compose interpolation](https://docs.docker.com/compose/how-tos/environment-variables/variable-interpolation/)
и [Compose Build](https://docs.docker.com/reference/compose-file/build/).

## 4. Запуск и состояние

```bash
docker compose --env-file deploy/.env -f deploy/compose.production.yaml up -d --build
docker compose --env-file deploy/.env -f deploy/compose.production.yaml ps -a
docker compose --env-file deploy/.env -f deploy/compose.production.yaml logs --tail=100 db api
```

БД проходит `pg_isready` healthcheck, затем API выполняет `alembic upgrade head`.
Только при успешных миграциях запускается
`uvicorn app.main:app --host 0.0.0.0 --port 8000 --root-path "$API_ROOT_PATH"`.
`exec` передаёт процессу
uvicorn сигналы остановки. Оба контейнера используют `restart: unless-stopped`.
Команда `up -d` сама по себе не подтверждает готовность API: проверьте состояние,
логи миграций и HTTP-запросы ниже. При циклических рестартах исправьте причину
из логов до дальнейшего использования.

Для наблюдения за запуском в реальном времени:

```bash
docker compose --env-file deploy/.env -f deploy/compose.production.yaml logs -f --tail=100 api
```

`Ctrl+C` завершает просмотр логов, контейнеры продолжают работать.

## 5. Проверка HTTP и доступа к БД

```bash
curl --fail-with-body --show-error http://127.0.0.1:5081/health
curl --fail-with-body --show-error http://127.0.0.1:5081/api/services
```

Оба запроса должны вернуть HTTP `200`. `/health` возвращает `{"status":"ok"}`
и сейчас проверяет только процесс приложения: он не обращается к PostgreSQL.
`/api/services` дополнительно выполняет запрос к БД и подтверждает доступ
к таблице сервисов после миграций. Без API-ключа возвращаются только сервисы
с `isPublic=true`, поэтому `[]` допустим даже при непустой БД. Успешного
`/health` недостаточно для подтверждения работы БД.

## 6. Остановка и повторный запуск

Остановить приложение вместе с БД и удалить контейнеры/сеть, сохранив данные:

```bash
docker compose --env-file deploy/.env -f deploy/compose.production.yaml down
```

Не добавляйте `-v` / `--volumes`, не удаляйте volume вручную и не запускайте
очистку Docker volumes: в `graphql-coverage_postgres-data` хранится БД.
Для остановки с сохранением самих контейнеров можно использовать `stop`:

```bash
docker compose --env-file deploy/.env -f deploy/compose.production.yaml stop
docker compose --env-file deploy/.env -f deploy/compose.production.yaml ps -a
```

Повторный `up -d --build` использует тот же volume и снова проверяет миграции.
Инициализационные переменные `POSTGRES_DB`, `POSTGRES_USER`, `POSTGRES_PASSWORD`
действуют только при первом создании БД в пустом volume. Изменение `.env` не
меняет пароль или пользователя уже созданной БД; для ротации нужен отдельный
согласованный шаг изменения учётных данных в PostgreSQL. Не удаляйте volume
ради исправления пароля. Перед обновлением приложения и применением новых
миграций сделайте резервную копию БД.
