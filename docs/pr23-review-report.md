# PR 23 — исправления по ревью

> Исторический документ: результаты, ограничения и планы относятся к указанной
> в нём базе. Действующие контракты — в [указателе документации](README.md).

База исправлений: `c06d6c14c9066584372382503c87416bf5125659`.
Изменения остаются в `codex/v2-access-contracts`; отдельный PR не создаётся.
Новых миграций нет, auth-миграция и Coverage Engine не меняются.

## Cookies и несколько сервисов

В browser session все request Cookie и upstream Set-Cookie блокируются, независимо
от имени, включая cookies других приложений общего origin и дублированные заголовки.
Cookie2/Set-Cookie2 не передаются. Клиент не может включить passthrough параметром/заголовком.
Даже валидный API key не разрешает cookies в запросе с Origin или Sec-Fetch-*.

Только явный доверенный API-key CLI/E2E без browser context сохраняет прежние
upstream Cookie/Set-Cookie. Coverage cookies и внутренние управляющие заголовки
удаляются всегда. Authorization и тело сохраняются; HTTPX jar создаётся на один exchange.
E2E должен разделять jars по сервисам/run; browser cookie-auth к чужому upstream
не поддерживается. Политика не содержит глобального cookie allowlist/jar и сохраняется
при будущей маршрутизации нескольких upstream.

## HTTPS за Nginx → Docker → Uvicorn

`app.server` явно включает Uvicorn proxy headers и требует точные непосредственные
peer IP из FORWARDED_ALLOW_IPS. Wildcard, CIDR и DNS-имена отвергаются.
Production Compose передаёт этот обязательный список и точный PUBLIC_ORIGIN,
фиксирует dev mode=false и проверяет настройки **до** Alembic/startup.
Пустой список не заменяется догадкой о Docker gateway.

[Сниппет Nginx](../deploy/nginx/coverage-proxy.conf) заменяет внешние XFP/XFF
реальными scheme/peer, удаляет forwarded host metadata и отключает retry mutation.
Это файл для ревью/применения оператором; Nginx на VPS не менялся.

Тесты используют реальный Uvicorn.Config.load/ProxyHeadersMiddleware и проверяют
HTTPS с доверенного Docker peer, отказ поддельному XFP с другого peer, игнорирование
Forwarded/X-Forwarded-Host, отказ при отсутствии HTTPS, login/cookie/CSRF/write/logout.

## Проверки

- Полный локальный pytest с PostgreSQL 18.6: **250 passed, 1 warning in 36.38s**.
  Включает 27 новых регрессионных сценариев, старые Coverage Engine/ACL/миграции.
- `pip check`: No broken requirements found.
- `git diff --check`, production и smoke `docker compose config --quiet`,
  `bash -n deploy/vps/graphql-coverage-deploy.sh`, compileall: успешно.
- `FORWARDED_ALLOW_IPS=172.30.8.1 python -m app.server --check-config`: успешно
  (адрес — только тестовый пример, не значение для production).
- Предупреждение pytest прежнее: StarletteDeprecationWarning о httpx TestClient;
  проверки не отключались.

Локальный Docker daemon недоступен, поэтому полный контейнерный smoke локально
не объявляется проверенным. В Backend CI добавлен отдельный обязательный шаг
`python tests/docker_auth_smoke.py`: PostgreSQL 16 + новый API image + Nginx TLS
в host network → host loopback port mapping → Docker NAT → Uvicorn. Используются
уникальный Compose project, self-signed тестовый TLS и тестовые credentials;
production origin — только значение Host/Origin, сеть идёт в loopback.
Проверяются login, Secure/HttpOnly cookie, CSRF/write/logout, перезапись внешних
forwarded headers и отказ другому непосредственному peer. Стенд удаляется в finally.
Результат этого шага фиксируется в описании PR после завершения CI.

## Merge и production

[Порядок слияния и выпуска](../deploy/PR23-RELEASE.md): ответственный оператор
**до merge** выключает ENABLE_PRODUCTION_DEPLOY и отменяет/дожидается уже
запущенных/ожидающих jobs. Затем review/CI, merge без deploy, отдельное
согласованное окно настройки точных peer IP/Nginx/backup/migrations/bootstrap,
HTTPS smoke и только затем возврат autodeploy. Сам workflow/root-owned deploy
script не используется как rollback к старому SHA: он допускает только fast-forward.

Автор исправлений не менял repository variables, VPS, production конфигурацию,
секреты или БД, не запускал deploy/workflow_dispatch и не выполнял merge.
Фактический transport peer и TLS-конфигурация VPS остаются проверкой оператора
в release window; тестовый Docker-стенд не заменяет эту проверку.

Ссылки на настройки: [Uvicorn](https://uvicorn.dev/settings/#http),
[Nginx proxy_set_header](https://nginx.org/en/docs/http/ngx_http_proxy_module.html#proxy_set_header),
[Docker port publishing](https://docs.docker.com/engine/network/port-publishing/).
