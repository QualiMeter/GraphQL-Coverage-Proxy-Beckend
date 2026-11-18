# GitLab launch_unknown: расследование и безопасная диагностика

База исправления: `main` @ `747480e` (PR #41, #42, #43 слиты).
Production, QA, frontend и GitLab CI не изменялись; реальные pipeline не запускались.

## Вывод и пределы доказательств

| Гипотеза | Подтверждено | Не подтверждено / как проверить |
| --- | --- | --- |
| H1: первый DNS IP недоступен, другой доступен | В исходном `PinnedBackend.connect_tcp()` выбран только `addresses[0]`. В воспроизведении с fake sockets отказ первого IP завершал запрос без попытки второго. Новые тесты проходят для обоих порядков IPv4/IPv6 и ConnectError/ConnectTimeout. | Нет DNS-ответов и TCP/TLS измерений из backend-контейнера VPS. **IPv6 не установлен как причина инцидента.** Нужны проверки ниже. |
| H2: POST принят, ответ потерян / таймаут | `gitlab_network_error` объединял отказ TCP, TLS, записи, чтения и внешний timeout. POST мог создать pipeline до потери ответа. В тесте отправленные байты POST, потеря ответа, `launch_unknown`, восстановление ID точной сверкой UUID воспроизведены вместе с PostgreSQL. | Судьба конкретного run/pipeline неизвестна. Нужны run UUID, время, сохранённый статус, GET сверка и безопасные логи. |
| Отказ Runner / CI rules | Ручной запуск ранее выполнял тесты и присылал GraphQL-запросы — свидетельство из описания инцидента, без независимой проверки VPS. | Нет свидетельств, что Runner или `.gitlab-ci.yml` вызвали сетевую ошибку backend. Исследовать их только после подтверждённого ответа GitLab / найденного pipeline. |

По данным задания и PDF причина на VPS пока **не установлена**. Нулевое покрытие
не отличает отказ TCP от не запущенных/ещё не начавшихся тестов. Кодовый дефект H1
исправлен как воспроизводимый риск доступности; это не доказательство, что именно
он вызвал наблюдавшийся инцидент. Таймауты не увеличены.

## Изменения и безопасность POST

- Все DNS-ответы по-прежнему проверяются до передачи credentials. Один запрещённый
  ответ отклоняет весь набор; повторного DNS lookup при fallback нет.
- На TCP ConnectError/ConnectTimeout перебираются только заранее проверенные
  числовые IP, каждый максимум один раз. Оставшееся время общего connect-бюджета
  делится между оставшимися адресами. Например, для двух адресов при 10 секундах
  первый получает максимум 5 секунд; неиспользованное время остаётся второму.
- Проверка фактического peer/IP/port остаётся обязательной. Несовпадение peer
  завершает запрос без fallback. TLS начинается после выхода из TCP-перебора:
  сохраняются исходные Host/SNI, hostname verification и доверенная цепочка CA.
- TLS failure, write/read error, HTTP error, потеря ответа и отмена не вызывают
  повторного подключения к другому IP или повторной отправки HTTP POST.
  HTTPcore `retries=0`, `trust_env=False`, `follow_redirects=False` сохранены.
- Лимиты остались: DNS 3 секунды, HTTPX фазовые timeout 10 секунд, общий request
  15 секунд, poll 60 секунд. Connect-бюджет общий для адресов; TLS имеет свой
  фазовый timeout и также ограничен общими 15 секундами.
- Все сетевые ошибки POST консервативно сохраняют `ambiguous=True`, даже при
  диагностически подтверждённом отказе до HTTP. Автоматических повторов нет.
  DNS/policy rejection остаётся однозначным отказом до передачи credentials.
- `launch_unknown` остаётся в существующем leased monitor. Сверка использует
  только GET: список `source=api`, ref, время и точный `COVERAGE_RUN_ID` из variables.
  После deadline сбор неполный, но сверка и карантин продолжаются. Логика
  сохранения результата, дедлайнов, страниц и аренды не изменена.

Диагностика использует существующую ограниченную асинхронную очередь:
`event=coverage_outbound`, фиксированные `stage`/`outcome`, method, status,
elapsed_ms, номер TCP-попытки, family=4/6, run_id (когда известен).
Нет URL/path, IP, ref, токена, cookies, Authorization, request/response body,
переменных pipeline, exception text или traceback. Для TCP fallback логируется
каждая неудача и успешная резервная попытка; HTTPcore tracing принимает только
внутренний callback, request extensions не могут подменить его или SNI.
Сбой/переполнение логгера не влияет на запрос. Очередь best effort: отсутствие
события само по себе не доказывает отсутствие запроса.

| stage | Значение |
| --- | --- |
| dns | Ошибка/таймаут разрешения до HTTP |
| policy | Отклонён URL или один из DNS-адресов |
| tcp | Установка сокета; attempt/family позволяют проверить fallback |
| tls | Handshake/проверка сертификата до HTTP |
| send_headers / send_body | Передача HTTP; результат POST потенциально неизвестен |
| response_headers / response_body | Ожидание/чтение ответа; POST мог уже выполниться |
| http | Получен ответ GitLab: status + rejected/invalid; тело не журналируется |
| complete | Успешно прочитан JSON (проверка бизнес-структуры выполняется следом) |
| request | Нет более точной стадии, например transport не выдал trace |
| reconcile | matched / not_found: один ограниченный проход фоновой сверки |

Внешний timeout на любой стадии показывает последнюю наблюдавшуюся стадию.
Не следует трактовать его как доказательство того, что GitLab не создал pipeline.
HTTPcore может после WriteError попытаться прочитать ответ сервера; событие
ошибки отправки сохраняется отдельно. Это чтение того же запроса, не новый POST.

## Read-only команды для VPS

Выполнять из каталога установленного backend. Войти в **существующий** контейнер:

```bash
docker compose --env-file deploy/.env -f deploy/compose.production.yaml exec api sh
```

Следующие Python-команды выполняются внутри контейнера из `/code` (рабочего
каталога образа). Они не создают pipeline, не меняют БД, env или конфигурацию.
Не включать `set -x`, HTTPX/httpcore DEBUG, `curl -v`, `printenv`, полный
`docker inspect` или `docker compose config` с выводом секретов.

### 1. DNS: порядок и оба семейства настроенного хоста

```bash
python - <<'PY'
import asyncio, ipaddress
from app.gitlab_config import gitlab_settings
from app.upstream import validate_endpoint, EndpointResolutionError, UnsafeEndpoint
async def main():
    try:
        config = gitlab_settings()
        for sample in range(1, 6):
            destination = await validate_endpoint(config.host)
            print({'sample': sample, 'host': destination.url.host,
                   'validated_addresses': [
                       {'ip': ip, 'family': ipaddress.ip_address(ip).version}
                       for ip in destination.addresses]})
    except EndpointResolutionError:
        print({'result': 'dns_error_or_timeout'})
    except UnsafeEndpoint:
        print({'result': 'policy_rejected_no_connections'})
    except Exception:
        print({'result': 'configuration_or_diagnostic_failure'})
asyncio.run(main())
PY
```

Это порядок проверенных DNS-адресов для каждой выборки, а не доказательство
порядка при прошлом инциденте. При policy rejection остановить сетевые пробы.

### 2. TCP и TLS отдельно для каждого проверенного IPv4/IPv6

Никакие HTTP-байты и credentials в этой пробе не отправляются. Числовой IP
фиксируется до подключения, peer проверяет тот же production `PinnedBackend`.
TLS проверяет исходное имя и сертификат без `verify=False`.

```bash
python - <<'PY'
import asyncio, ipaddress, ssl, time
import httpcore
from app.gitlab_config import gitlab_settings
from app.upstream import Destination, PinnedBackend, validate_endpoint
async def main():
    try:
        dest = await validate_endpoint(gitlab_settings().host)
    except Exception:
        print({'result': 'validation_failed_no_connections'}); return
    for ip in dest.addresses:
        stream, stage, started = None, 'tcp', time.monotonic()
        result = {'ip': ip, 'family': ipaddress.ip_address(ip).version}
        try:
            backend = PinnedBackend(Destination(dest.url, (ip,)))
            stream = await backend.connect_tcp(dest.url.raw_host.decode('ascii'),
                                               dest.url.port or 443, timeout=5)
            result['tcp_ms'] = round((time.monotonic() - started) * 1000)
            stage, started = 'tls', time.monotonic()
            stream = await stream.start_tls(ssl.create_default_context(),
                                           server_hostname=dest.url.host, timeout=5)
            result.update(tls_ms=round((time.monotonic() - started) * 1000), result='ok')
        except (httpcore.TimeoutException, TimeoutError):
            result.update(result='timeout', stage=stage)
        except Exception:
            result.update(result='error', stage=stage)
        finally:
            if stream is not None:
                try: await stream.aclose()
                except Exception: pass
        print(result)
asyncio.run(main())
PY
```

Если первый адрес стабильно падает, а второй проходит TCP/TLS, H1 подтверждается
для текущей сети. Сопоставить со временем исходного сбоя; не отключать IPv6 вслепую.

### 3. GitLab API GET без создания pipeline

Используется production GitLabClient с теми же pinning/TLS/таймаутами.
Печатаются только количество веток и безопасный код ошибки; token берётся
из env внутри процесса, не передаётся аргументом командной строки.

```bash
python - <<'PY'
import asyncio
from app.gitlab_client import GitLabClient, GitLabError
from app.gitlab_config import gitlab_settings
async def main():
    try:
        config = gitlab_settings()
        client = GitLabClient(config)
        for service_id, project in config.projects.items():
            try:
                branches = await client.branches(project, 1)
                print({'service_id': str(service_id), 'project_id': project,
                       'result': 'ok', 'branches_on_first_page': len(branches)})
            except GitLabError as exc:
                print({'service_id': str(service_id), 'result': exc.code})
    except Exception:
        print({'result': 'configuration_or_diagnostic_failure'})
asyncio.run(main())
PY
```

### 4. Существующий проблемный Coverage Run и GET-сверка его UUID

Заменить только UUID. Первая часть — `SELECT` в read-only транзакции: сохранённые
статусы, расписание, deadline, число ошибок, страница и активность аренды.
Вторая — один read-only проход существующего reconcile; возвращённый ID **не
записывается в БД этой командой**. При двух кандидатах и отсутствии совпадения
нужно продолжить со следующей сохранённой страницей штатным monitor; один
`not_found` не доказывает отсутствие pipeline.

```bash
RUN_ID='00000000-0000-0000-0000-000000000000' python - <<'PY'
import asyncio, os, uuid
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from app.gitlab_client import GitLabClient, GitLabError
from app.gitlab_config import gitlab_settings
async def main():
    engine = None
    try:
        run_id = uuid.UUID(os.environ['RUN_ID'])
        engine = create_async_engine(os.environ['DATABASE_URL'], echo=False)
        async with engine.connect() as conn:
            async with conn.begin():
                await conn.execute(text('SET TRANSACTION READ ONLY'))
                row = (await conn.execute(text('''
                    SELECT p.run_id, p.pipeline_id, p.pipeline_status, p.error_code,
                           r.status AS run_status, p.created_at, p.finished_at,
                           p.collection_incomplete, p.next_check_at, p.deadline_at,
                           p.reconcile_page, p.check_failures,
                           (p.lease_until > now()) AS lease_active,
                           p.gitlab_host, p.project_id, p.ref
                    FROM gitlab_pipelines p JOIN test_runs r ON r.id = p.run_id
                    WHERE p.run_id = :run_id
                '''), {'run_id': run_id})).mappings().one_or_none()
        if row is None:
            print({'result': 'run_not_found'}); return
        private = {'gitlab_host', 'ref'}
        saved = {k: (v.isoformat() if hasattr(v, 'isoformat') else str(v)
                     if isinstance(v, uuid.UUID) else v)
                 for k, v in row.items() if k not in private}
        saved['tracking_finished'] = row['finished_at'] is not None
        print(saved)
        config = gitlab_settings()
        if row['gitlab_host'] != config.host or row['project_id'] not in config.projects.values():
            print({'result': 'configuration_changed_no_api_request'}); return
        client = GitLabClient(config)
        if row['pipeline_id'] is None:
            info, next_page = await client.reconcile(row['project_id'], row['ref'], run_id,
                                                     row['created_at'], row['reconcile_page'])
            print({'result': 'matched' if info else 'not_found_on_this_page',
                   'pipeline_id': info.id if info else None, 'next_page': next_page})
        else:
            info = await client.pipeline(row['project_id'], row['pipeline_id'])
            print({'result': 'known_pipeline', 'pipeline_id': info.id, 'status': info.status})
    except GitLabError as exc:
        print({'result': exc.code})
    except Exception:
        print({'result': 'diagnostic_failed_no_exception_details'})
    finally:
        if engine is not None: await engine.dispose()
asyncio.run(main())
PY
```

Также в авторизованном интерфейсе/API выполнить `GET /api/runs/<runId>/gitlab`:
сопоставить pipelineId, pipelineStatus, errorCode, runStatus, trackingFinished,
collectionIncomplete. GET не делает новый запуск. Не вставлять cookies/ключи
в shell history. До выяснения исхода не повторять `POST /api/runs/gitlab`.

### 5. Безопасная выборка логов monitor (на хосте VPS)

Выйти из контейнера (`exit`). Команда выводит только разрешённые поля JSON-событий,
не сырые строки журналов. До этого исправления детальная стадия сетевого сбоя
в журналах отсутствовала; задним числом восстановить её по errorCode невозможно.

```bash
docker compose --env-file deploy/.env -f deploy/compose.production.yaml logs \
  --since 2h --no-log-prefix api 2>/dev/null | python3 -c '
import json, sys
allowed = {"event", "stage", "outcome", "method", "status", "elapsed_ms",
           "attempt", "family", "run_id", "exception_type"}
for line in sys.stdin:
    try:
        e = json.loads(line)
    except (ValueError, TypeError):
        continue
    if e.get("event") == "coverage_outbound" or (
        e.get("event") == "coverage_internal_failure" and
        e.get("stage", "").startswith("gitlab_")):
        print(json.dumps({k: v for k, v in e.items() if k in allowed}))
'
```

Проверить следующие несколько циклов (poll + backoff до 300 секунд): меняются ли
next_check_at/reconcile_page/check_failures, появляются ли GET errors или
`reconcile=not_found/matched`. Живая аренда длится до 90 секунд. `gitlab_monitor`
сообщает о непредвиденной ошибке фонового цикла; `gitlab_configuration_changed`
требует сверить серверную конфигурацию. 401/403 на variables/list означают проблему
доступа, а не доказательство отсутствия pipeline. Ничего не исправлять прямым UPDATE.

## Проверки

Новые автоматические сценарии находятся в `tests/test_gitlab_network.py` и
`tests/test_gitlab_runs_postgres.py`. Используются fake DNS, fake TCP/TLS streams
и MockTransport, реальных GitLab POST нет. Покрыты fallback обоих семейств,
все IP недоступны, общий бюджет, отмена, peer mismatch, mixed private/public DNS,
TLS/send/read/общий timeout, единственная отправка POST, защищённый trace,
безопасные события/сломанный sink и восстановление ID через настоящий клиент,
сверку UUID и PostgreSQL. Существующие тесты redirects, SSRF, GitLab, GraphQL
Proxy, схемы и аналитики входят в полную матрицу.

Результаты полной матрицы и CI фиксируются в описании PR после выполнения.

## Действия на VPS и один контролируемый E2E

1. **До обновления** выполнить read-only диагностику выше и выяснить судьбу
   старого run. Сохранить обезличенные результаты (без секретов/variables).
   Предоставить их для подтверждения или опровержения H1/H2. Не увеличивать
   таймаут, не отключать TLS/IPv6/SSRF и не снимать карантин вслепую.
2. После ревью и разрешения владельца развернуть проверенный commit штатным
   процессом. В этом PR нет миграций и изменений CI. Production этой работой
   не обновлялся. Сначала повторить DNS/TCP/TLS/GET и убедиться, что monitor
   продолжает сверять старый UUID и сохраняет найденный pipelineId.
3. Если исход старого запуска остаётся неизвестен, остановиться на диагностике.
   Не создавать новый run на том же snapshot. Подтверждённое отсутствие pipeline
   оформлять только существующим операторским процессом из `gitlab-e2e.md`.
4. **Только после отдельного разрешения на реальный запуск** выбрать известные
   service/snapshot и защищённую E2E-ветку. Проверить GET branches и отсутствие
   активного/неразрешённого запуска. В интерфейсе выполнить ровно один запуск;
   записать UTC-время, runId, pipelineId и pipelineUrl. При `launch_unknown`
   больше не нажимать «Запустить», наблюдать GET и monitor.
5. В GitLab убедиться, что pipeline с `source=api` и этим COVERAGE_RUN_ID ровно
   один. Не публиковать variables. Проверить jobs, поступление GraphQL-запросов
   именно в этот runId, рост requestCount/покрытия. Дождаться terminal pipeline
   и завершения отслеживания. Для завершённого вовремя сбора ожидаются
   `runStatus=completed`, `trackingFinished=true`, `collectionIncomplete=false`;
   success/failed тестов проверяется отдельно через pipelineStatus.
6. Если ответа нет — сопоставить stage/method/elapsed_ms и run_id, проверить
   GET reconcile и доступ к variables. Не повторять POST. Только измеренное
   превышение 10/15 секунд при исправной сети обосновывает отдельное изменение
   timeout с проверкой poll/lease бюджетов.
