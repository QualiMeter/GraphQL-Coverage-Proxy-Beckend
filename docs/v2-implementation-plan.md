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
