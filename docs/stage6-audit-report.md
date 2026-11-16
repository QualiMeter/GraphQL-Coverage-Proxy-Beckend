# Этап 6: исправления независимого аудита

> Исторический документ: результаты, ограничения и планы относятся к указанной
> в нём базе. Действующие контракты — в [указателе документации](README.md).

База: актуальный `main` проверен перед началом работы —
`21737e85c158e041363f72836fa6e04f94ce4b08`.
Ветка: `fix/stage6-audit-findings`.

## Исправления

1. **P1, PostgreSQL pool exhaustion.** `/graphql` использует отдельную короткую
   сессию проверки browser principal. После проверки grants, run, snapshot и
   service proxy закрывает read session до DNS, чтения тела и HTTP. Закрытие
   сбрасывает ORM session и возвращает соединение; сохранение после upstream
   начинает новую транзакцию. `record_observation` по-прежнему повторно проверяет
   status под `FOR UPDATE`, синхронизируясь с `finish_run`. Размер пула, роли,
   Origin/CSRF, модель grants, обработка ошибок и отсутствие upstream retries сохранены.
2. **P2, RunOut.metadata.** Все три API-маршрута (`GET /api/runs`,
   `POST /api/runs`, `POST /api/runs/{runId}/finish`) явно передают
   `include_metadata=principal.admin`. Viewer/analyst/operator получают `{}`
   в разрешённых ответах; admin и доверенный CLI получают исходный JSON.
   Содержимое metadata в PostgreSQL и права на создание/завершение не меняются.
3. **P2, длинные operation names.** `observed_requests.operation_name` переведён
   с `VARCHAR(255)` на `TEXT`; соответствующий лимит фильтра журнала удалён.
   DTO и анализатор уже принимают полное имя. `query_hash`, `operationKey`,
   точная фильтрация, группировка и формула покрытия сохранены.

## Изменённые файлы

| Файл | Изменение |
|---|---|
| `app/proxy.py` | Короткая авторизация, освобождение preflight connection до сети |
| `app/api.py`, `app/services.py` | Явная политика metadata для create/finish |
| `app/models.py`, `app/journal.py` | Полное имя операции и фильтр |
| `alembic/versions/20261119_operation_name_text.py` | Новая миграция с защитой downgrade |
| `tests/test_stage6_audit_postgres.py` | 21 регрессионный тест на PostgreSQL |
| `README.md`, `docs/frontend-history-requests.md`, этот отчёт | Фактическое поведение и миграция |

## Регрессионные тесты

- 15 параллельных запросов со стандартным пулом 5+10; API key и cookie session;
  отдельные остановки на DNS, чтении тела и upstream. Пока запросы ожидают,
  checkouts равны нулю и `GET /api/services` отвечает. После отпускания все 15
  наблюдений и field usages сохранены с правильной identity.
- Завершение run во время upstream mutation для обоих способов авторизации:
  finish завершается, поздняя запись отклонена без наблюдения/coverage и
  повторного upstream. Отдельно проверен настоящий сбой commit после mutation.
- Admin создаёт run с приватным маркером; пять типов principal проверяют
  историю, finish и create. Проверены отсутствие grant, ограничения ролей,
  cookie/Origin/CSRF, сохранность JSON и дальнейшее чтение admin.
- Query и mutation с именами 255, 256 и 1024 символов, а также соседними
  именами с дополнительным суффиксом: HTTP 200, один upstream на запрос,
  полный текст имени, прежние hash/key, точные фильтры журнала и группировка.
- Upgrade схемы с историческими наблюдениями, null/255-символьными именами;
  безопасный downgrade/upgrade; отказ downgrade при 256 символах с полным
  сохранением строк, типа столбца и Alembic revision.

Проверка против неизменённого исходного main воспроизвела все три дефекта:
9 выбранных regression cases завершились ожидаемыми ошибками (6 pool,
operator metadata, query/mutation с именем 256). На исправлениях проходят все 21.

## Проверки

Локально: Python 3.12.3, PostgreSQL 18.6 в новом кластере
`/tmp/graphql-stage6-pg`, только `127.0.0.1:55486`. Приложение и тесты используют
разные тестовые БД; migration/concurrency tests создают дополнительные временные БД.

| Проверка | Результат |
|---|---|
| Полный unit-набор | 332 passed |
| Полный PostgreSQL integration-набор | 191 passed, без skip |
| Новые regression tests отдельно | 21 passed |
| Миграция на схеме с данными / безопасный и отклонённый downgrade | passed |
| `alembic check` | No new upgrade operations detected |
| `python -m pip check` | No broken requirements found |
| `docker compose config --quiet` | passed |
| `bash -n deploy/vps/graphql-coverage-deploy.sh` | passed |
| `git diff --check` | passed |
| Локальный Docker/Nginx auth smoke | Попытка не выполнена: Docker daemon недоступен |
| CI на HEAD PR | Ссылка и окончательный результат фиксируются в описании PR; CI также запускает Docker/Nginx smoke на PostgreSQL 16 |

Первый unit-запуск внутри сетевого sandbox завис и был прерван; повторный
полный запуск вне sandbox прошёл. Успешные PostgreSQL проверки выполнялись
с разрешённым доступом исключительно к изолированному локальному кластеру.

## Миграция и ограничения

Новая ревизия `20261119_operation_name_text` следует за
`20261118_final_features`. Старые миграции не изменены. Применение:
`alembic upgrade head` в целевой среде при отдельном одобренном release.
Переход в `TEXT` не меняет существующие наблюдения или operation keys.

Downgrade получает `ACCESS EXCLUSIVE` на `observed_requests` и проверяет
`char_length(operation_name) > 255` в той же транзакции. При наличии длинных имён
откат прерывается без обрезания или удаления данных; остаётся новая revision.
Для отката требуется отдельное решение о сохранении таких наблюдений.
DDL требует блокировки таблицы, поэтому окно миграции выбирается при release.

Существующая гонка завершения run с уже отправленной mutation сохраняет
предусмотренный контракт: mutation может выполниться, а поздняя запись получает
502 с предупреждением; автоматического повтора нет. Локальный Docker smoke
не подтверждён из-за недоступного daemon; итог CI указан в PR.
Merge, deploy, включение `ENABLE_PRODUCTION_DEPLOY` и обращения к production БД
не выполнялись. Отложенные P2-функции и история этапов 1–5 не изменены.

## Доработка PR #28 после независимого ревью: DNS сервисных маршрутов

База доработки: `ce81f36f85ecf5bfade75798df369fdad9541200`.
Ревью подтвердило три первоначальных исправления и выявило существовавшее
ранее удержание PostgreSQL connections при DNS в POST/PATCH сервисов.

`post_service` и `patch_service` теперь получают session factory, а их cookie
authentication использует существующую короткую `introspection_principal`.
POST проверяет роль/Origin/CSRF до DNS; PATCH дополнительно проверяет ACL в
короткой read session. Все эти contexts закрыты до `validate_endpoint`.
DNS и проверка всех полученных IP остаются прежними и выполняются без checkout.

После DNS новая session повторно вызывает `current_principal` и `require_operator`.
POST создаёт сервис и owner grant с актуальным principal в одной транзакции.
PATCH передаёт callback авторизации в `update_service`: он выполняется **после
получения `FOR UPDATE`**, перед проверкой активных runs и изменением любых полей.
Поэтому отзыв grant/сессии/роли во время DNS или ожидания service lock не
разрешает запись. Недоступный сервис по-прежнему даёт 404; активный run блокирует
смену endpoint с 409. Создание run и обновление endpoint сохраняют общий service lock.
PATCH без endpoint не вызывает DNS; повторное сохранение того же endpoint
допускается при активном run, как и раньше. Общая `current_principal` в
`app/auth.py`, размеры пула, pool timeout и SSRF-политика не менялись.

Изменённые файлы этой доработки:

- `app/api.py`: отдельные фазы сервисных writes и повторная авторизация.
- `app/services.py`: callback авторизации после блокировки в `update_service`.
- `tests/test_service_dns_transactions_postgres.py`: 20 новых regression cases.
- `tests/test_access_control_postgres.py`, `tests/test_run_analytics_postgres.py`:
  factory override для новых сессий на существующем rollback-only connection;
  прежние проверки не ослаблены.
- `README.md`, этот отчёт: фактическое поведение и результаты.

Новые тесты используют настоящий login, cookie, hashed session и CSRF, реальный
изолированный PostgreSQL и **неизменённые defaults SQLAlchemy: 5 + 10, timeout 30 с**.
Контролируемый Event приостанавливает только resolver; production
`validate_endpoint` проверяет URL и каждый возвращённый IP.

Три обязательных stress-сценария — POST cookie, PATCH CLI, PATCH cookie —
проверяют 15 одновременных DNS waits, `checkedout() == 0`, независимый GET,
отсутствие изменений до DNS release и сохранность всех writes/owner grants после.
Дополнительно проверены отзыв grant и сессии, смена роли, отключение пользователя,
старт run до записи, удаление сервиса, приоритет ACL над конфликтом run,
отмена DNS-запроса, смешанный public/private DNS answer, ошибка commit после
flush, отсутствие частичных service/grant writes, повторный ключ и PATCH без
смены endpoint. Отдельный тест отзывает grant, пока PATCH ожидает service lock.
Проверки читают фактические строки и JSON PostgreSQL.

На исходном HEAD все 3 stress-теста воспроизводят дефект: **15 checkouts во время
DNS**. После исправления новые тесты: **20 passed**; целевая проверка вместе со
старыми ACL/analytics tests: **30 passed**. Финальный полный pytest: **543 passed,
без skip** (332 unit, 211 PostgreSQL integration); в полный набор входят все 21
прежних регрессии этапа 6. Результат CI на новом HEAD указан в описании PR.
`alembic upgrade head` и `alembic check` проходят,
schema drift отсутствует; новых миграций нет. `pip check`, Compose config,
синтаксис deploy-скрипта и `git diff --check` проходят.

Остаётся обычная семантика конкурентных коротких транзакций: запись проверяет
права в момент финальной авторизации под service lock. Изменение grants после
этой проверки не отменяет уже разрешённую транзакцию задним числом. При отмене
во время DNS запись ещё не начиналась. Merge/deploy не выполнялись; Docker/Nginx
smoke и окончательные результаты GitHub CI фиксируются в описании PR.
