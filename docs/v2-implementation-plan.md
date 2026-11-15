# План реализации ТЗ v2.0

База аудита: `main`, `3e2dff5387120a12e94e6171d431293741a77340`.
Источник: `GraphQL_Coverage_Proxy_Backend_Integration_TZ_v2.docx`, версия 2.0,
15 ноября 2026; инструкции заказчика и последующее уточнение безопасного AST
имеют приоритет над альтернативами в документе. P2 исключён.

## Проверенная основа

Coverage Engine считает выбранные поля при `success AND response_has_data=true`;
HTTP/GraphQL-ошибки эту семантику не заменяют. Существуют семь таблиц,
две additive-миграции, AST-анализатор, HTTP proxy, SDL/introspection,
публичность сервисов и агрегаты operations/stats. Дедупликация схем —
`service_id + schema_hash`; operationKey — SHA-256 JSON tuple
`[query_hash, operationName, operationType]`. Эти алгоритмы сохраняются.

Локальная исходная ветка `chore/backend-ci` отставала от main и содержала
незакоммиченный `docs/e2e-integration.md`; она не изменялась. Работа ведётся
в отдельной копии `.worktrees/integration-v2`, ветка `codex/v2-access-contracts`.

На момент аудита единственный открытый PR —
[#22](https://github.com/QualiMeter/GraphQL-Coverage-Proxy-Beckend/pull/22),
`638d14838ee54e5ef2825179ba3d16d9adea22db`. Он добавляет один тест
`test_control_scenario_coverage_through_proxy` в `tests/test_proxy_e2e_postgres.py`:
5/9 после query, 6/9 после mutation, повторный query повышает hits.
Тест не дублируется в реализации; его patch проверяется отдельно поверх ветки.
В PR сообщено о падении Swagger: базовый прогон в текущем окружении его
не воспроизвёл — 192 passed на PostgreSQL 18.6. Проверки не отключались.

## Расхождения и последовательность PR

| Этап | Расхождение | Результат |
|---|---|---|
| 1. Доступ и контракты | Только общий административный ключ, нет пользователей/сессий/ACL | Настоящие пользователи, роли, серверные сессии, CSRF, bootstrap CLI, применение ACL к существующим маршрутам |
| 2. Multi-service и proxy | Глобальный upstream, нет endpointUrl/SSRF | Nullable endpoint, description, updatedAt; CRUD; блокировка смены endpoint при активном run; DNS pinning, лимиты, маршрутизация run → snapshot → service |
| 3. Схемы | Introspection глобальная, ограниченные сведения схемы | Introspection выбранного сервиса с общей SSRF-защитой, SDL endpoint, displayName, описания/аргументы/deprecation/связи |
| 4. История и обращения | История только по snapshot; журнал только limit/offset | История serviceId, стабильные фильтры/сортировки, массив + X-Total-Count; request detail с полями точного snapshot |
| 5. Статистика и безопасный документ | Нет analysisStatusCounts/P95; документ не хранится | SQL-счётчики, percentile_cont; безопасная копия реального AST, литералы маскированы, fragments/directives сохранены, выдача по ACL |
| 6. Итоговые проверки и документация | Нет полного multi-service сценария | PostgreSQL, два upstream и параллельных run, SSRF/rebinding, отсутствие секретов в БД/логах/API, согласованные OpenAPI/DTO и отчёт frontend |

Каждый этап — работоспособный PR для ревью, без самостоятельного merge и деплоя.
До принятия первого этапа остальная P0/P1-функциональность не объявляется готовой.

## Зафиксированные решения

- Браузер: same-origin cookie session; CLI/E2E сохраняет серверный API key.
- Роли глобальные, доступ к конкретным сервисам задаётся отдельным grant.
  Публикация даёт чтение агрегатов, но не журнал и не управление.
- Run create/finish и proxy: operator/admin. Analyst читает журнал при наличии grant.
- Изменение endpoint блокируется при любом активном run этого сервиса;
  run и смена конфигурации должны синхронизироваться блокировкой сервиса.
- Никакого произвольного выбора сервиса без run ID; временный legacy passthrough
  будет явно настраиваться в этапе 2. В этапе 1 сохранён прежний CLI passthrough;
  browser proxy уже требует run ID.
- HTTPS в production; HTTP/локальные адреса только explicit dev mode с allowlist.
  Валидация при сохранении и соединении, проверка всех DNS-ответов и pinning IP;
  redirects off, credentials/query/fragment запрещены; bounded body/time/concurrency.
- После upstream mutation сбой наблюдения не вызывает повтор upstream;
  контракт должен явно отражать, что запрос мог выполниться.
- Новое уточнение заказчика: безопасный GraphQL-код входит в P1.
  Только AST-копия реально выполненного документа, без значений variables,
  токенов/cookies/ответов; литералы/defaults маскируются; при невозможности
  безопасной обработки — `documentAvailability: unavailable`.
  query_hash/operationKey не меняются. Никакого восстановления query из selectedFields.
- Сравнение прогонов, P50/P99, HTTP-распределения и error timeline — P2, позже.

## Этап 2: фактическая база и граница PR

После принятия #23 база — main `bf9cdb2cdbcbdc7d7fd2f4e41b10ea8f910c641a`,
ветка `codex/v2-multi-service`. Реализованы endpoint CRUD, nullable-миграция,
service lock для run create / endpoint update, pinned DNS transport, явный
legacy fallback, limits и multi-service PostgreSQL/TCP проверки.
PR #22 остаётся открытым с тем же HEAD `638d14838ee54e5ef2825179ba3d16d9adea22db`;
его контрольный тест отдельно проходит поверх этапа 2, не дублируется в ветке.

Introspection выбранного endpoint относится к этапу 3. Чтобы глобальная схема
не попала в configured-сервис до этого этапа, маршрут временно возвращает 409
и предлагает SDL. Старый путь доступен только explicit legacy mode и использует
общую SSRF-защиту. Остальные история/аналитика/P95/безопасный AST и P2 не добавлялись.
[Frontend delta](frontend-multi-service.md), [SSRF](multi-service-security.md),
[E2E](multi-service-e2e.md), [будущий release](../deploy/STAGE2-RELEASE.md).

## Этап 3: service-aware introspection

База main 09659aed51f91dd6e93d95a3818497901c96349c (PR #24), ветка
codex/v2-service-introspection. Временный запрет configured-service introspection
снят: endpoint выбирает Service, используется существующий DNS-pinned transport.
Nullable endpoint сохраняет только explicit legacy fallback. Новых миграций нет.
Dedup/enrichment атомарны под service lock/savepoint; endpoint change во время
fetch отклоняет запись, конкурентные SDL/introspection возвращают один snapshot.
[Frontend delta](frontend-introspection.md), [архитектура/лимиты/upstream auth](service-introspection.md).
SDL download/displayName/расширенные schema DTO, история и P1-аналитика остаются
последующими работами; P2 не начат.


## Этап 4: история и журнал обращений — реализовано для ревью

База main `979c5f9b9078834a92261b53fdc28e9fdff87916` (принятый PR #25),
ветка `codex/v2-history-requests`. История ровно по serviceId/snapshotId,
UTC/status filters, стабильная SQL pagination, X-Total-Count, без скрытого limit
для старых клиентов. Request journal: типизированные AND-фильтры, snapshot-scoped
EXISTS, operationKey, count, NULLS LAST, detail с реальными selectedFields и
countedInCoverage. ACL и закрытые metadata сохранены; агрегаты/coverage не менялись.
Additive migration `20261117_history_journal`: nullable материализованный ключ по
**прежней** формуле, backfill исторических записей bounded batches, четыре индекса.
[Frontend контракт и эксплуатационные ограничения](frontend-history-requests.md).

PostgreSQL-тесты проверяют несколько snapshots/сервисов, legacy DTO/pagination,
UTC-границы, одинаковые timestamps, operation identities, отсутствие дублей,
чужие requestId/snapshot fields, все роли и отзыв прав, миграцию с 1004 записями.
Merge/deploy выполняет заказчик после ревью; production deploy не включался.

На этап 5 остаются analysisStatusCounts/P95 и безопасная копия реального AST с
маскированием литералов/defaults/fragments/directives и проверкой отсутствия секретов.
Ранее отложенные schema DTO/SDL download/displayName из полного ТЗ не объявляются
завершёнными: их нужно явно включить в согласованный остаток P0/P1 на этапах 5–6.
Этап 6 — итоговый аудит P0/P1, сквозные проверки и окончательная документация.
P2 не начат. Переход к этапу 5 только после ревью и принятия этапа 4.


## Этап 5: финальные функциональные P0/P1 — реализовано для ревью

База main `1fd1ffdce3946217e15e8bd1f2d01010d911212f` (принятый PR #26),
ветка `codex/v2-final-features`. [Frontend delta](frontend-final-features.md),
[release/migration notes](../deploy/STAGE5-RELEASE.md).

| Приоритет | Функциональный критерий | Фактическая реализация |
|---|---|---|
| P0 | Несколько сервисов, CRUD, независимые upstream/runs | Принятые этапы 1–2; service row lock, endpoint 409 при активном run, pinned DNS/SSRF, legacy только явно |
| P0 | Сессии, роли, CSRF, grants и приватность | Принятый этап 1; journal требует отдельный grant независимо от публикации, CLI key не в браузере |
| P0 | SDL/introspection выбранного сервиса, dedup/атомарность | Принятый этап 3; раздельные short transactions и ACL/endpoint recheck; SDL body/ошибки/имена дополнительно ограничены |
| P0 | История всех snapshots сервиса, фильтры и total | Принятый этап 4; без скрытого limit старого snapshotId-вызова |
| P0 | Журнал, AND-фильтры, operationKey/detail/точный snapshot | Принятый этап 4; без duplicate/N+1, SQL pagination, protected detail |
| P0 | analysisStatusCounts | Один SQL aggregate для всех пяти реальных категорий и total; нули для пустого run |
| P1 | P95 и среднее/пустые значения | percentile_cont(0.95), NULL исключены, run isolation; avg/timeline сохранены |
| P1 | Реальный безопасный документ | AST всего wire document, все scalar literals/defaults masked; bounded text, fail-closed unavailable; только detail по ACL |
| P1 | SDL и schema metadata | SDL выбранного snapshot; GraphField description/deprecationReason/args и safe defaults, GraphType descriptions/root roles/interfaces/possibleTypes, stable relation UUID |
| P1 | Snapshot label | Optional displayName + PATCH только operator/grant/admin/CLI; hash/ID/coverage неизменны |

Additive migration `20261118_final_features`: nullable display_name и safe document,
non-null document_availability с default unavailable и CHECK состояния/размера.
Нет UPDATE исторического SDL/hash/IDs и восстановления прежних query.
Открытый PR #22 остаётся отдельным тестом, не дублируется в ветке.

Функциональные пункты P0/P1 выше реализованы, но **полная приёмка backend ещё не
объявляется завершённой**: нужны независимое ревью этого PR и этап 6. Открытые
проверки: сопоставить итоговый checklist ТЗ со всеми принятыми PR; проверить
frontend end-to-end, production observability без body/credential capture,
sizing safe documents и план всех pending migrations на копии production БД.
После принятия этапа 5 функциональных P0/P1 endpoints в плане не остаётся.
Не заменять unsupported fragments/subscriptions успешным coverage: улучшение
Coverage Engine не входит в запрос и формула не меняется.

Этап 6 — независимый аудит, сквозные проверки/документация и отдельно согласованный
release plan; VPS пока не меняется. P2 не начат. После PR этапа 5 остановиться
для ревью, не выполнять merge/deploy и не включать ENABLE_PRODUCTION_DEPLOY.

Локальная верификация финального этапа 5: PostgreSQL 18.6 / graphql-core 3.3.2 —
501 passed, без skipped, один прежний Starlette/httpx warning. Дополнительно
safe AST/schema/PostgreSQL набор на graphql-core 3.2.14 — 51 passed; контрольный
сценарий открытого PR #22 — 1 passed, 17 deselected. Alembic check, pip check,
Compose config, deploy-script syntax, compileall и diff check пройдены.
Результат GitHub CI указывается в описании PR после его завершения.
