# Исправления независимого аудита Sol

База: main `41db524295e5cbf27a100bd417f62ddc896b30d6` (проверена повторно перед
публикацией). Один PR; исходные замечания — три логических коммита, дополнительное ревью
конкуренции — отдельные последующие коммиты. Без merge/deploy/production changes.
Path / Depth / RelationHits не изменялись.

## B1: неполный GitLab run

Deadline любого незавершённого сбора даёт TestRun.failed, сохраняет observations
и устанавливает устойчивый `gitlab_pipelines.collection_incomplete`. API GitLab
аддитивно возвращает `collectionIncomplete`. `pipelineStatus` отражает исход
тестов отдельно от полноты сбора; `trackingFinished` — завершённость сверки.
Первый terminal-ответ после deadline также консервативно считается неполным.
Поздний success не очищает признак и deadline-код и не меняет failed в completed.
Сравнение отклоняет failed/неполные runs с 409 RUN_NOT_COMPLETED.

Durable intent, один POST, lease token, reconciliation и quarantine сохранены.
Сверка продолжается после deadline. Карантин снимается только при достоверном
terminal status либо существующей операторской процедуре подтверждения отсутствия.
Нормальные terminal success/failed/canceled/skipped до deadline сохраняют прежний
контракт completed (исход тестов — отдельный pipelineStatus).

Миграция `20261123_incomplete_collection` после `20261122_field_evidence` добавляет
boolean NOT NULL DEFAULT false. Backfill выполняется только при прямых признаках:
сохранённый tracking_deadline_exceeded либо completed run с незакрытой pipeline
связью. Они получают collection_incomplete=true, deadline-код и failed. Observations,
finished_at и quarantine не переписываются. Downgrade не восстанавливает completed.

Ограничение истории: прежний terminal poll мог очистить deadline-код. Если связь
уже закрыта и прямые признаки утрачены, надёжно отличить неполный run от нормального
невозможно. Такие записи не меняются автоматически; нужна внешняя проверка по
GitLab/журналам. Время само по себе не используется для догадочного backfill.

## B2: CPU comparison вне event loop

Пять пакетных SELECT сохраняют один REPEATABLE READ READ ONLY snapshot, включая
проверку доступа. Материализуются только scalar rows. Затем API закрывает транзакцию
и сессию, возвращает соединение в пул и передаёт данные в чистую функцию построения
результата через bounded comparison_work (максимум два отдельных comparison worker на процесс).
AsyncSession/соединения/ORM-объекты в поток не передаются. Ожидание worker также
происходит без DB connection. schema_from_sdl и матрица field × run выполняются в
worker. Ограничение capacity при cancellation переиспользует общий безопасный механизм.

Фильтры, пагинация, сортировка, категории и числовые результаты сохранены.
Интеграционный тест выполняет четыре одновременных comparison по 45 001 полю и
20 runs с pool_size=1: два активных worker, свободное соединение во время CPU,
работающий health, неизменные пять SELECT на comparison, проверка результатов.
Прежние nonzero coverage/delta/fragment-evidence проверки также сохранены.

Воспроизведение измерения (только на disposable DB с применёнными миграциями):

```bash
AUDIT_DATABASE_URL=postgresql+asyncpg://... python tests/benchmark_comparison.py
# SOL_SOURCE=/path/to/audited/checkout выбирает исходный код для before.
```

Сценарий: 406 913 байт SDL, 45 001 OBJECT field, 501 OBJECT type, 20 completed runs,
fields.limit=types.limit=1, пустые observations. Две верхнеуровневые конкурентные
задачи: comparison и 5 ms heartbeat с GET /health через HTTPX ASGITransport.
Результаты проверяются: 45 001 neverCovered/stillUncovered, нулевое покрытие и
request counts всех 20 runs, корректные totals/pages. Это локальное ASGI измерение,
без внешнего HTTP/TLS; не обещание production latency или ускорения.

Исходное последовательное измерение для `e7527a4`, без параллельного pytest:

| Кэш / версия | Comparison, с | Max event-loop gap, с | Max health request, с | Max health от назначенного времени, с |
| --- | ---: | ---: | ---: | ---: |
| cold / before | 2.869599 | 2.623245 | 0.052319 | 2.619330 |
| cold / after | 3.871480 | 0.194967 | 0.154782 | 0.191013 |
| warm / before | 1.106645 | 0.745494 | 0.002793 | 0.741591 |
| warm / after | 1.016268 | 0.279353 | 0.027653 | 0.275286 |

Все четыре ответа — HTTP 200 с корректными результатами. Прежний health request
показывает только время после фактического запуска; scheduled timing включает
задержку его запуска из-за занятого event loop. На холодном кэше общая длительность
выросла; ускорения comparison не заявляем.

Worker threads устраняют выполнение тяжёлой функции непосредственно в event loop.
Остаются GIL, GC, SQL-декодирование и общая конкуренция CPU: жёсткой гарантии
latency нет. Общее время comparison может вырасти. Health handler timing отдельно
от scheduled timing: первое не включает ожидание первоначального планирования.

## B3 и документация

Общий RunStatus = running/completed/failed используется в history filter и run DTO.
API-тесты каждого статуса проверяют service/snapshot scope, стабильные страницы и
X-Total-Count. OpenAPI и история обновлены.
RequestDetail теперь корректно описывает safeGraphqlDocument: сырые GraphQL body и
variables не сохраняются; безопасный нормализованный документ может сохраняться,
исходные секреты и значения аргументов не раскрываются. README согласован.

## Проверки исходного HEAD e7527a4

- Все unit tests: **621 passed**, 351 deselected, 33.54 s.
- Все PostgreSQL integration tests: **351 passed**, 621 deselected, 285.85 s.
- Итого **972 passed**, **0 skipped**, **0 failed**. Единственное предупреждение
  в каждом запуске — существующая Starlette TestClient/httpx deprecation.
- GitLab targeted: 36 passed, 11.69 s; comparison/evidence/migration targeted:
  32 passed, 27.54 s. Эти тесты входят и в итоговый полный набор.
- Alembic upgrade head, downgrade/upgrade на изолированной исторической БД,
  backfill/observation preservation и check: успешно; No new upgrade operations detected.
- pip check, compileall, docker compose config --quiet, bash -n deploy script,
  git diff --check: успешно.

Полный набор включает GraphQL Proxy, прежний database-pool P1, SSRF/forwarding/
Authorization, GitLab lifecycle/lease/recovery/concurrency, comparison, selected vs
observed evidence, Fragments, service ACL и изоляцию пользователей. PR #33–#36 уже
входят в проверенную базу; их regression suites проходят. Изменения comparison
не меняют selected hits в тестах с различными evidence.

Локальный Docker smoke был вызван, но Docker daemon недоступен
(`/home/alexei/.docker/desktop/docker.sock` отсутствует); этот этап проверяется
отдельно в GitHub Actions Backend CI на PostgreSQL 16. Локальный PostgreSQL — 18.
Статус окончательного CI см. в checks PR. Production deploy job для PR не запускается.

## Изменённые файлы

- `alembic/versions/20261123_incomplete_collection.py`
- `app/gitlab_runs.py`, `app/gitlab_api.py`, `app/models.py`
- `app/run_comparison.py`, `app/api.py`, `app/e2e_capture.py`, `app/schema_work.py`
- `app/contracts.py`, `app/api_models.py`, `app/journal.py`
- `tests/test_gitlab_runs_postgres.py`, `tests/test_incomplete_migration_postgres.py`
- `tests/test_comparison_workers_postgres.py`, `tests/benchmark_comparison.py`
- `tests/test_capture_comparison_contention_postgres.py`, `tests/test_schema_work.py`
- `tests/test_run_analytics_postgres.py`, `tests/test_fragment_field_evidence_postgres.py`
- `tests/test_history_journal_postgres.py`, `tests/test_api_contract.py`
- `README.md`, `docs/gitlab-e2e.md`, `docs/frontend-history-requests.md`, `docs/sol-audit-fixes.md`

## Дополнительное ревью: конкуренция comparison и capture

Проблема **подтверждена** на исходном HEAD `e7527a4e80d5e2d8cf7f55f604f84e3458a88097`.
Два comparison занимали общие schema_work slots. Capture повторно открывал read
transaction в load_run_snapshot после upstream и держал её во время analyzer и
ожидания response/error/evidence CPU work. В контрольном тесте с прогретой именно
сохранённой SDL за 5 секунд только 2 из 4 capture достигли CPU queue; оба соединения
pool_size=2/max_overflow=0 оставались checked out, остальные запросы ждали pool.
Один и тот же тест на исходном коде падает, после исправления проходит.

Исправление состоит из двух частей:

- Capture копирует неизменяемую SDL в scalar string и закрывает свою read-сессию
  до normalize/analyzer и любого ожидания CPU. record_observation открывает свежую
  транзакцию, блокирует и повторно проверяет run. Атомарность observations/usage/
  evidence и защита от finish во время анализа сохранены.
- Comparison использует отдельный ThreadPoolExecutor(max_workers=2) и собственный
  лимит ожидания. MAX_SCHEMA_WORKERS остаётся 2. Comparison не занимает capture slots
  и не встаёт перед его analyzer в default executor. AsyncSession не передаётся в
  потоки. Суммарное число comparison-потоков ограничено на процесс; они создаются
  лениво. Контекст запроса сохраняется через copy_context.

Shield и done callback обоих лимитов оставляют slot занятым до фактического
окончания потока. Отменённый запрос не записывает результат. Проверены отмена
работающего/ожидающего worker, ошибки worker, сохранение contextvars, последующее
использование capacity и отсутствие DB checkouts после отмены.

Новый CI regression: 45 001 поле, 20 completed runs, два реально загруженных через
API comparison с удержанными CPU workers, четыре GraphQL HTTP capture, PostgreSQL
pool_size=2/max_overflow=0. Capture workers также остановлены барьером: проверяется
нулевой checkout даже когда два capture вычисляются и остальные ожидают их лимит.
Затем capture завершается **до** освобождения comparison workers. Это управляемый
concurrency-тест, а не обещание production latency.

| Сценарий | Все 4 capture в очереди, с | /health, с | /stats, с | DB checkout в очереди / после | Итог observations |
| --- | ---: | ---: | ---: | --- | --- |
| Обычный | 0.159894 | 0.006274 | 0.045109 | 0 / 0 | 4 |
| Отмена 4 capture и 2 comparison, затем новый capture | 0.139832 | 0.001184 | 0.039772 | 0 / 0 | 1 новый, 0 отменённых |

В обычном сценарии сохранены 8 observed + 4 indeterminate evidence и по одной
GraphQL error на observation. После отмены сохранены только 2 observed + 1
indeterminate свежего capture. Число upstream-запросов равно числу HTTP-запросов:
повторной отправки нет. Comparison сохраняет 45 001 neverCovered для исходных пустых
runs; последующий comparison также завершается после отмены без потери slots.

Целевые новые integration-тесты: **2 passed, 26.57 s**. Они автоматически входят
в существующий `pytest -m integration` в Backend CI. Публичный API и схема БД
дополнительно не менялись. Полный финальный набор и CI указаны в описании PR.

Финальная локальная регрессия после доработки: **624 unit passed** (33.87 s) +
**353 PostgreSQL integration passed** (316.32 s) = **977 passed, 0 skipped, 0 failed**.
Единственное предупреждение — прежняя Starlette TestClient/httpx deprecation.
Compileall, pip check, Docker Compose config и git diff --check прошли.
Новых миграций нет. Финальный CI для нового HEAD и вердикт см. в PR #38.
