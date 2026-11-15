# Этап 6: исправления независимого аудита

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
