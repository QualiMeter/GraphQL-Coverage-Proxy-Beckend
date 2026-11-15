# Production Pre-Deployment Checklist

Эта инструкция предназначена **оператору после независимого ревью PR**. Агент
не выполнял SSH, установку, merge или deployment. `ENABLE_PRODUCTION_DEPLOY`
остаётся включённым; Secrets, Variables и workflow не меняются.
Для этого обновления порядок ниже имеет приоритет над первоначальной установкой
в `AUTODEPLOY.md`: **сначала установка скрипта, потом merge**.

## 1. Предварительные условия

- Ревью и обязательный CI завершены успешно; ответственным согласовано окно
  замены скрипта. Известны текущий успешный Actions run и развёрнутый SHA из его
  журнала; один Git HEAD не доказывает успешный deployment.
- Во время установки никто не делает push/merge в `main`, не запускает
  `workflow_dispatch` и не повторяет старые deployment jobs. В Actions нет
  queued/in-progress/waiting backend/deploy runs, которые могут начать deployment.
  Это короткая организационная блокировка выпуска, без переключения Variables.
- Оператор имеет root-доступ, установленный read-only Git-доступ к репозиторию,
  исправный Docker/Compose и доступ к журналу Actions. Не вводить токены в команды,
  URL, `.env` или отчёт. При отсутствии доступа остановиться.
- Рабочий каталог `/opt/graphql-coverage` остаётся на `main`, tracked-файлы чисты;
  установленный скрипт — обычный root-owned файл, не symlink. Существующий
  PostgreSQL, Nginx, контейнеры и volumes не изменяются при подготовке.

Проверенный исходный коммит кода/тестов:
`70848471e7359b7d775eae0f4918393667227000`.
Следующий коммит этого PR содержит только инструкции и отчёт; байты скрипта
совпадают с указанным коммитом. Если ревью изменит скрипт, **остановиться и заново
зафиксировать/проверить SHA и SHA256**; эти значения больше неприменимы.

## 2. Подготовить защищённого кандидата (действующая версия не меняется)

Выполнить в отдельной root Bash-сессии на VPS. Не включать `set -x`.
Команды не обновляют production checkout и не запускают deployment.
HTTPS fetch использует уже настроенный доступ; при ошибке аутентификации
остановиться, не добавлять credentials в URL.

```bash
sudo -i
bash
set -Eeuo pipefail
set +x
umask 077
readonly REVIEWED_SHA=70848471e7359b7d775eae0f4918393667227000
readonly SCRIPT_SHA256=bd83ef8ce231621556fc2a5205464930a98924c3265777e2bdd5007f3a3f304d
readonly STAGE="/root/graphql-coverage-script-$REVIEWED_SHA"
readonly LIVE=/usr/local/sbin/graphql-coverage-deploy
test ! -e "$STAGE"
install -d -o root -g root -m 0700 "$STAGE"
git init --bare --quiet "$STAGE/source.git"
git --git-dir="$STAGE/source.git" remote add origin \
  https://github.com/QualiMeter/GraphQL-Coverage-Proxy-Beckend.git
git --git-dir="$STAGE/source.git" fetch --no-tags --depth=1 origin "$REVIEWED_SHA"
test "$(git --git-dir="$STAGE/source.git" rev-parse FETCH_HEAD)" = "$REVIEWED_SHA"
git --git-dir="$STAGE/source.git" show \
  "$REVIEWED_SHA:deploy/vps/graphql-coverage-deploy.sh" > "$STAGE/candidate.source"
install -o root -g root -m 0750 "$STAGE/candidate.source" "$STAGE/candidate"
printf '%s  %s\n' "$SCRIPT_SHA256" "$STAGE/candidate" | sha256sum --check --strict
bash -n "$STAGE/candidate"
test ! -L "$STAGE/candidate"
test "$(stat -c '%U:%G:%a' "$STAGE")" = root:root:700
test "$(stat -c '%U:%G:%a' "$STAGE/candidate")" = root:root:750
stat -c '%U:%G:%a %n' "$STAGE" "$STAGE/candidate"
```

Ожидается: checksum `OK`, `bash -n` с кодом 0, `root:root:700` для каталога,
`root:root:750` для кандидата. Действующий файл пока не заменён. Остановиться при
любом несовпадении, изменении SHA на ревью, недоступности Git или неясном статусе
production. Не выполнять `git pull`, `git reset` или запуск кандидата «для проверки»:
без записи успеха это инициирует настоящий deployment.

## 3. Установить только после ревью, под тем же deployment lock

Продолжить в той же root Bash-сессии. Сначала подтвердить в Actions отсутствие
активных/ожидающих выпусков и удерживать организационную блокировку из пункта 1.
`flock -n` не ждёт завершения чужого deployment: если занят, остановиться и
разобраться. Не обходить lock и не удалять его файл. Блокировка удерживается
до конца проверок установки; GitHub Actions, попавший в это окно, получит отказ,
а не параллельный deployment. Не перезапускать такой job до освобождения lock.

```bash
exec 9>/run/lock/graphql-coverage-deploy.lock
flock -n 9

test -f "$LIVE" && test ! -L "$LIVE"
test "$(stat -c '%U:%G:%a' "$LIVE")" = root:root:750
bash -n "$LIVE"
test "$(git -C /opt/graphql-coverage branch --show-current)" = main
test -z "$(git -C /opt/graphql-coverage status --porcelain --untracked-files=no)"
readonly CHECKOUT_BEFORE="$(git -C /opt/graphql-coverage rev-parse HEAD)"
compose() {
  docker compose --env-file /opt/graphql-coverage/deploy/.env \
    -f /opt/graphql-coverage/deploy/compose.production.yaml "$@"
}
compose config --quiet
readonly API_CONTAINER_BEFORE="$(compose ps -q api)"
test -n "$API_CONTAINER_BEFORE"
readonly API_STARTED_BEFORE="$(docker inspect -f '{{.State.StartedAt}}' "$API_CONTAINER_BEFORE")"
readonly BOUND="$(compose port api 8000 | tail -n 1)"
[[ "$BOUND" =~ ^127\.0\.0\.1:([0-9]+)$ ]]
readonly API_PORT="${BASH_REMATCH[1]}"
curl --fail --silent --show-error --max-time 5 "http://127.0.0.1:$API_PORT/health" >/dev/null
curl --fail --silent --show-error --max-time 5 "http://127.0.0.1:$API_PORT/api/services" >/dev/null

# Сохранить точный путь резервной копии в журнале оператора.
test ! -e "$STAGE/previous"
cp --preserve=mode,ownership,timestamps -- "$LIVE" "$STAGE/previous"
sha256sum "$STAGE/previous" > "$STAGE/previous.sha256"
bash -n "$STAGE/previous"
test "$(stat -c '%U:%G:%a' "$STAGE/previous")" = root:root:750

# Временный файл в той же файловой системе, затем атомарная замена.
next_script="$(mktemp /usr/local/sbin/.graphql-coverage-deploy.XXXXXXXX)"
install -o root -g root -m 0750 "$STAGE/candidate" "$next_script"
printf '%s  %s\n' "$SCRIPT_SHA256" "$next_script" | sha256sum --check --strict
bash -n "$next_script"
mv -fT -- "$next_script" "$LIVE"
printf '%s  %s\n' "$SCRIPT_SHA256" "$LIVE" | sha256sum --check --strict
bash -n "$LIVE"
test "$(stat -c '%U:%G:%a' "$LIVE")" = root:root:750
```

Скрипт использует прежние Compose paths/commands и не зависит от новых Python
модулей. Его установка совместима с уже работающим backend: файл сам по себе
ничего не запускает. Краткий период «скрипт новее `main`» допустим.

## 4. Инициализация состояния и точка готовности к merge

Новая запись: `/var/lib/graphql-coverage-deploy/last-successful-sha`, каталог
root:root `0700`, файл `0600`. **Не создавать запись вручную из Git HEAD.**
Инициализация не требуется: при отсутствии записи следующий автоматический
deployment выполнит backup/build/up/readiness и атомарно запишет новый SHA.
Это безопасно и для повтора текущего SHA. Не вызывать deployment-скрипт сейчас.
Если state-каталог неожиданно уже существует, остановиться и выяснить его
происхождение; не удалять и не подменять существующую запись. Штатные повреждённые
записи скрипт обрабатывает как необходимость полного повторного deployment.

```bash
test ! -e /var/lib/graphql-coverage-deploy
test "$(git -C /opt/graphql-coverage rev-parse HEAD)" = "$CHECKOUT_BEFORE"
test "$(compose ps -q api)" = "$API_CONTAINER_BEFORE"
test "$(docker inspect -f '{{.State.StartedAt}}' "$API_CONTAINER_BEFORE")" = "$API_STARTED_BEFORE"
curl --fail --silent --show-error --max-time 5 "http://127.0.0.1:$API_PORT/health" >/dev/null
curl --fail --silent --show-error --max-time 5 "http://127.0.0.1:$API_PORT/api/services" >/dev/null
printf '%s  %s\n' "$SCRIPT_SHA256" "$LIVE" | sha256sum --check --strict
sha256sum --check --strict "$STAGE/previous.sha256"
flock -u 9
exec 9>&-
```

**Merge разрешён только теперь:** ревью/CI зелёные на финальном PR SHA,
установленный файл имеет ожидаемый checksum/владельца/права, backup скрипта
проверен, HEAD и работающий контейнер не изменились, health/readiness проходят,
lock освобождён, в Actions нет старых ожидающих deployment, оператор сообщил
готовность ответственному за merge. После этого можно снять организационную
блокировку и выполнить merge. Переключать `ENABLE_PRODUCTION_DEPLOY` не нужно.

## 5. Ошибка установки / восстановление предыдущего скрипта

При любой ошибке не выполнять merge, не запускать приложение заново, сохранить
журналы без секретов. Если root-shell завершился из-за `set -e`, lock уже
освобождён: перед восстановлением снова убедиться в отсутствии Actions runs и
снова получить тот же lock. Если lock занят — остановиться. Если предыдущая
версия уже известна как неисправная, восстановление файла не даёт разрешения
на выпуск: она содержит исходный дефект повторного deployment.

В новой root Bash-сессии (тот же путь STAGE), под организационной блокировкой:

```bash
set -Eeuo pipefail
set +x
umask 077
STAGE=/root/graphql-coverage-script-70848471e7359b7d775eae0f4918393667227000
LIVE=/usr/local/sbin/graphql-coverage-deploy
exec 9>/run/lock/graphql-coverage-deploy.lock
flock -n 9
sha256sum --check --strict "$STAGE/previous.sha256"
bash -n "$STAGE/previous"
restore_script="$(mktemp /usr/local/sbin/.graphql-coverage-deploy.restore.XXXXXXXX)"
install -o root -g root -m 0750 "$STAGE/previous" "$restore_script"
mv -fT -- "$restore_script" "$LIVE"
cmp --silent "$STAGE/previous" "$LIVE"
bash -n "$LIVE"
test "$(stat -c '%U:%G:%a' "$LIVE")" = root:root:750
flock -u 9
exec 9>&-
```

Повторить read-only readiness из пункта 3. Эта операция восстанавливает **только
установленный скрипт**, не checkout, API image, данные или состояние миграций.
Она не откатывает уже выполненный deployment. При сбое deployment после merge
сохранить PostgreSQL backup, исследовать API logs оператором; исправить причину
и повторить проверенный SHA или выпустить последующий fast-forward commit.
Автоматического восстановления БД нет; не применять `down -v`, не удалять volumes
и не запускать старый SHA для отката. Не повторять job старым неисправным скриптом.

## 6. После автоматического deployment

- Проверить GitHub Actions backend/deploy jobs именно merge-коммита `main`:
  build/up и обе readiness-проверки успешны; backup PostgreSQL проверен и сохранён.
- В read-only root-сессии проверить (EXPECTED_SHA — полный SHA из этого run):

```bash
EXPECTED_SHA='<полный SHA успешно проверенного merge-коммита из Actions>'
[[ "$EXPECTED_SHA" =~ ^[0-9a-f]{40}$ ]]
test "$(git -C /opt/graphql-coverage rev-parse HEAD)" = "$EXPECTED_SHA"
test "$(cat /var/lib/graphql-coverage-deploy/last-successful-sha)" = "$EXPECTED_SHA"
test "$(stat -c '%U:%G:%a' /var/lib/graphql-coverage-deploy)" = root:root:700
test "$(stat -c '%U:%G:%a' /var/lib/graphql-coverage-deploy/last-successful-sha)" = root:root:600
```

- Повторить определение loopback port и оба `curl` из пункта 3; проверить внешнюю
  доступность через действующий Nginx и привычный browser login/coverage/stats.
  Не печатать ответы приватных API, cookies, `.env`, полную Compose-конфигурацию
  или секреты в отчёт.
- Сохранить root-owned копию предыдущего скрипта и PostgreSQL backup согласно
  действующей политике хранения. Ручной контроль SHA/marker, состояния API,
  обычных операций импорта/coverage/stats и журнала первого autodeploy обязателен.
