# Порядок слияния и выпуска PR 23

> Процедура конкретного исторического выпуска. Сохранена для миграций, rollback
> и безопасности; её SHA, состояние autodeploy и предварительные условия нужно
> подтвердить отдельно. Общая конфигурация — [README.md](README.md).

Слияние в main может автоматически запустить выкладку. CI в репозитории содержит
job deploy: после успешного backend, только для push/workflow_dispatch на main,
если repository variable `ENABLE_PRODUCTION_DEPLOY == 'true'`.
PR job сам по себе production не разворачивает. Значение production variable и
конфигурация VPS в рамках исправлений не читались и не менялись.

Этот документ — инструкция ответственному оператору; команды production
не выполнялись автором PR. Слияние, изменение GitHub variable, Nginx, deploy/.env,
миграции, bootstrap и выкладку выполняет команда отдельным согласованным действием.

## До слияния

1. Ответственный оператор проверяет Settings → Secrets and variables → Actions → Variables.
   Считать автодеплой включённым, пока обратное не подтверждено. **До merge** установить
   `ENABLE_PRODUCTION_DEPLOY=false`. Остановить/дождаться уже запущенных и ожидающих
   production jobs: изменение variable не отменяет job, который уже начал выполнение.
   Не запускать workflow_dispatch на main для «проверки» при включённом автодеплое.
2. Зафиксировать одобренный HEAD PR и дождаться зелёного CI именно для него,
   включая PostgreSQL 16 и Nginx/Docker/Uvicorn auth smoke. Проверить отсутствие
   изменившихся зависимостей/конфликтов main и согласовать окно выпуска.
3. Подготовить настройки в staging. Определить **фактический непосредственный peer**,
   который видит API-контейнер для соединения host Nginx → 127.0.0.1:5081 → Docker.
   Это часто bridge gateway, но не обязательно 127.0.0.1 и не универсальный 172.x IP.
   `docker network inspect --format '{{(index .IPAM.Config 0).Gateway}}' graphql-coverage_backend`
   даёт кандидата; подтвердить raw transport peer на staging без Uvicorn proxy headers,
   например временным диагностическим HTTP listener в той же Docker-сети.
   Не добавлять публичный diagnostic endpoint в приложение и не публиковать полный inspect/.env.
4. Подготовить точный `FORWARDED_ALLOW_IPS` (один адрес или перечень адресов),
   `COVERAGE_PUBLIC_ORIGIN=https://graphql.rebyte.fun`, `API_ROOT_PATH=`.
   Новая production Compose требует FORWARDED_ALLOW_IPS; отсутствие настройки
   остановит config/startup. `*`, CIDR, DNS-имена, unspecified/multicast запрещены.
   Не заменять точный список всей Docker/RFC1918-сетью. При смене сети peer проверяется заново.
5. Проверить diff существующего TLS server Nginx с [coverage-proxy.conf](nginx/coverage-proxy.conf).
   Каждый API/proxy location должен перезаписывать X-Forwarded-Proto через `$scheme`
   и X-Forwarded-For через `$remote_addr`, удалять Forwarded/forwarded host metadata.
   Не использовать `$http_x_forwarded_proto` или `$proxy_add_x_forwarded_for` для этой
   одношаговой схемы. HTTP listener только перенаправляет на HTTPS. Если используется
   real_ip, он тоже должен доверять только известному предыдущему proxy, а не произвольному клиенту.

## Слияние без немедленного деплоя

После согласования reviewer сливает PR при выключенном autodeploy. Проверить,
что workflow для merge commit выполнил backend, а deploy **skipped**.
Отметить SHA merge commit: при squash/rebase он отличается от HEAD PR.
Не считать merge разрешением на автоматическое изменение VPS.

## Отдельное согласованное окно выкладки

1. Снять backup БД и защищённую копию deploy/.env; зафиксировать текущий production SHA
   и образ для rollback. Существующий root-owned deploy script делает pg_dump и
   проверяет backup перед обновлением кода; проверить установленную версию скрипта,
   а не предполагать, что локальный файл автоматически её заменяет.
2. В согласованном окне оператор устанавливает подготовленные точные настройки в
   deploy/.env, проверяет Nginx (`nginx -t`) и применяет проверенную конфигурацию.
   API остаётся опубликован только на host loopback. Host и контейнеры с доступом
   к этому сокету входят в доверенную инфраструктуру; не присоединять к backend-сети
   посторонние приложения. IP allowlist не заменяет изоляцию host/network.
3. После получения принятого кода проверить `docker compose --env-file deploy/.env -f
   deploy/compose.production.yaml config --quiet`, собрать образ и выполнить в новом
   образе `python -m app.server --check-config` до пересоздания API.
   Production Compose фиксирует COVERAGE_DEV_MODE=false. Ключ API и пароль БД
   переиспользуются из защищённого окружения; PR не требует публиковать/перегенерировать их.
4. По отдельному разрешению выполнить существующую controlled deployment процедуру
   для проверенного main SHA. Startup сначала проверяет точные proxy IP, затем
   Alembic upgrade, затем запускает Uvicorn с proxy_headers=True и этим списком.
   Additive auth-миграция не меняет исторические таблицы. Некорректная настройка
   может оставить новый API недоступным — это не гарантированно безостановочный выпуск.
5. После миграции через интерактивный `docker compose ... exec api python -m app.auth_cli
   bootstrap-admin initialadmin` создать первого admin и назначить grants.
   Пароль только через getpass, без argv/env/logs. Открытой HTTP регистрации нет.
6. Через HTTPS проверить login, Secure/HttpOnly/SameSite cookie, session, CSRF,
   logout, приватность и журнал. Проверки /health и /api/services из deploy script
   сами по себе **не проверяют** HTTPS/session/CSRF. Проверить, что внешний поддельный
   X-Forwarded-Proto не меняет реальный TLS scheme и чужой Origin отвергается.

## Возврат автодеплоя и rollback

Включать ENABLE_PRODUCTION_DEPLOY=true только после приёмки выпуска, при отсутствии
старых ожидающих jobs: они могут прочитать variable позднее. Само включение variable
не является командой deploy; workflow_dispatch/push на main после него уже могут развернуть код.

При сбое оставить автодеплой выключенным. Root-owned script допускает только
fast-forward, поэтому запуск workflow на старый SHA **не является rollback**.
Возврат к предыдущему принятому образу/коду выполняется оператором по отдельному
плану. Additive auth-таблицы можно оставить: старая версия их игнорирует.
Не делать автоматический Alembic downgrade или восстановление всей БД: это может
удалить новые аккаунты/сессии и более свежие observations. Полный restore требует
отдельного решения о потере данных и согласованного окна.
