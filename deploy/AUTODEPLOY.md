# Автодеплой GraphQL Coverage на VPS

После настройки GitHub Actions развёртывает только проверенные коммиты из `main`.
До подготовки SSH и Secrets процесс **отключён**: переменная
`ENABLE_PRODUCTION_DEPLOY` отсутствует или не равна `true`.

Используются существующие `/opt/graphql-coverage`, `deploy/.env`,
`deploy/compose.production.yaml` и Docker. PostgreSQL volume, Nginx и Vitrina
не меняются. Приложение по-прежнему слушает только `127.0.0.1:5081`.

## Принцип — как у Vitrina

- Тесты до деплоя; никакого SSH на pull request.
- Отдельный `deploy` пользователь, root-owned скрипт и ограниченная команда sudo.
- Проверка SSH host key (`StrictHostKeyChecking=yes`).
- Отдельные SSH secrets и variables **этого** репозитория.
- Backup PostgreSQL до миграций; проверки API после запуска.

Секреты Vitrina не появляются автоматически в другом репозитории.
Лучше использовать отдельный SSH-ключ для GraphQL Coverage.

## 1. Установить серверный скрипт

Только **после мержа и ревью** PR. На VPS под root:

```bash
cd /opt/graphql-coverage
git status --short
git branch --show-current
git pull --ff-only origin main
install -o root -g root -m 0750 \
  deploy/vps/graphql-coverage-deploy.sh \
  /usr/local/sbin/graphql-coverage-deploy
bash -n /usr/local/sbin/graphql-coverage-deploy
```

Рабочее дерево должно быть чистым, ветка — `main`. Не используйте
`git reset --hard` и не пересоздавайте Docker volume.

Скрипт запускается из `/usr/local/sbin`; GitHub Actions не может автоматически
перезаписать эту root-owned копию. Изменения скрипта требуют нового ревью и
ручной переустановки.

## 2. Права пользователя deploy

Vitrina уже использует `deploy` на этом VPS. Проверьте:

```bash
id deploy
visudo -f /etc/sudoers.d/graphql-coverage-deploy
```

В файл sudoers добавьте **только**:

```text
deploy ALL=(root) NOPASSWD: /usr/local/sbin/graphql-coverage-deploy *
```

Затем:

```bash
chmod 0440 /etc/sudoers.d/graphql-coverage-deploy
visudo -cf /etc/sudoers.d/graphql-coverage-deploy
```

Сам скрипт проверяет аргумент (строго 40 шестнадцатеричных символов SHA),
каталог, ветку, чистоту Git и использует фиксированные пути.
Не выдавайте пользователю `deploy` общий sudo или доступ к Docker-группе.
Если `deploy` отсутствует, сначала создайте отдельную сервисную учётную запись.

## 3. Ключ GitHub Actions

На **локальном доверенном ПК**, не на VPS:

```bash
ssh-keygen -t ed25519 -a 100 -N '' \
  -C 'github-actions-graphql-coverage' \
  -f ./graphql_coverage_deploy
```

Публичный ключ `graphql_coverage_deploy.pub` добавьте новой строкой в
`/home/deploy/.ssh/authorized_keys`, не удаляя ключи Vitrina. Можно указать
перед публичным ключом SSH-ограничение `restrict`.

На VPS при необходимости подготовьте права:

```bash
install -d -o deploy -g deploy -m 0700 /home/deploy/.ssh
touch /home/deploy/.ssh/authorized_keys
chown deploy:deploy /home/deploy/.ssh/authorized_keys
chmod 0600 /home/deploy/.ssh/authorized_keys
```

**Приватный** `graphql_coverage_deploy` не отправляйте в чат и не коммитьте.

## 4. Проверить host key

На VPS:

```bash
ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub
```

На доверенном ПК:

```bash
ssh-keyscan -p 22 -t ed25519 <VPS_IP>
```

Сверьте отпечаток `ssh-keyscan` с отпечатком, полученным непосредственно
на VPS. Не отключайте проверку host key.

## 5. Secrets и Variables репозитория

Откройте `QualiMeter/GraphQL-Coverage-Proxy-Beckend → Settings →
Secrets and variables → Actions`.

Repository variables:

| Имя | Значение |
| --- | --- |
| `DEPLOY_HOST` | IP/DNS VPS, совпадающий с проверенным host key |
| `DEPLOY_USER` | `deploy` |
| `DEPLOY_PORT` | `22` (или реальный SSH-порт) |
| `ENABLE_PRODUCTION_DEPLOY` | пока не задавать; в самом конце поставить `true` |

Repository secrets:

| Имя | Значение |
| --- | --- |
| `DEPLOY_SSH_KEY` | полный приватный SSH-ключ `graphql_coverage_deploy` |
| `DEPLOY_KNOWN_HOSTS` | проверенная строка SSH host key |

`deploy/.env` остаётся **только на сервере**, в GitHub не копируется.
До мержа изменения с защитой API установите в серверном `deploy/.env`
`COVERAGE_API_KEY` (фактическое значение сгенерируйте командой
`openssl rand -hex 32` и не печатайте в логах/чате). Скрипт деплоя пересоздаст API-контейнер
с этой переменной; при отсутствующем ключе привилегированные маршруты
намеренно возвращают `503`, а публичное чтение доступно только для
явно опубликованных сервисов.

## 6. Проверка и включение

После установки скрипта, прав и SSH Secrets задайте
`ENABLE_PRODUCTION_DEPLOY=true`. В GitHub нажмите
`Actions → Backend CI → Run workflow → main`.

Workflow:

1. Поднимает тестовый PostgreSQL, применяет Alembic, запускает unit/integration.
2. Подключается к VPS только после успешных тестов.
3. Проверяет SHA и fast-forward, создаёт `pg_dump -Fc` в
   `/root/graphql-coverage-backups`, проверяет его через `pg_restore -l`.
4. Собирает образ и пересоздаёт **только api**, не трогая контейнер БД.
5. Ждёт успешных `/health` и `/api/services` на loopback.

Дальше каждый merge/push в `main` запускает ту же цепочку автоматически.

На VPS проверьте:

```bash
cd /opt/graphql-coverage
git log -1 --oneline
docker compose --env-file deploy/.env -f deploy/compose.production.yaml ps
curl -fsS http://127.0.0.1:5081/health
curl -fsS http://127.0.0.1:5081/api/services
```

## 7. Отказ и восстановление

Скрипт не удаляет volume и **не делает автоматический PostgreSQL restore**:
после миграций это могло бы уничтожить новые пользовательские данные.
При сбое изучайте логи контейнера и выполняйте ручное восстановление только
после выбора точной резервной копии. Проверенные дампы остаются в
`/root/graphql-coverage-backups`; политику хранения настроим отдельно.

Nginx, HTTPS, публичный `/coverage/` и защита административных эндпоинтов
остаются отдельной задачей. **Не публикуйте порт 5081 наружу.**
