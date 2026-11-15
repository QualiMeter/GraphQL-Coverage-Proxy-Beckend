# Этап 5: миграция и план будущего release (не команда на deploy)

Ветка `codex/v2-final-features`, база main `1fd1ffdce3946217e15e8bd1f2d01010d911212f`.
В этом PR production не меняется; ENABLE_PRODUCTION_DEPLOY остаётся выключенным.
Merge и VPS release выполняются заказчиком только после независимого ревью.
P2 отсутствует. [DTO и политика safe AST](../docs/frontend-final-features.md).

## Новая Alembic revision

`20261118_final_features` после `20261117_history_journal`:

- `schema_snapshots.display_name VARCHAR(255) NULL`;
- `observed_requests.safe_graphql_document TEXT NULL`;
- `observed_requests.document_availability VARCHAR(16) NOT NULL DEFAULT 'unavailable'`;
- CHECK: unavailable требует NULL document; available/redacted требуют ненулевой
  text размером 1–65536 UTF-8 bytes; других состояний нет.

Нет backfill AST, смены historical hashes/IDs/SDL/type/field/run данных.
Метаданные исторических схем читаются из их SDL, новые колонки descriptions/args
и новые индексы не нужны. P95 использует имеющийся run/duration index этапа 4.
Старые observations автоматически читаются как unavailable/null, label — null.
Действующие API-клиенты получают дополнительные read fields. PATCH label отдельный,
по прежним ролям/CSRF; POST introspection не меняет свой lifecycle и контракт.

Новые ограничения: safe normalization до 256 KiB input/10k tokens/20k nodes/depth64,
stored до 64 KiB. Превышение влияет только на display availability, не forwarding.
SDL JSON body до 1 MiB, label JSON до 16 KiB; ошибки без исходного текста.
Типы/поля сохраняемых схем ≤255 символов, не больше 5000 типов в GraphQL type_map (включая служебные)/50000 полей.

## Безопасный порядок для будущего rollout

1. Оставить `ENABLE_PRODUCTION_DEPLOY` выключенным до и после merge; PR job deploy
   также gated main/push и не должен запускаться от pull_request. Не включать его
   ради проверки CI. Проверить полный CI для review HEAD, выполнить независимое ревью.
2. Снять backup; узнать текущую Alembic revision VPS read-only и восстановить backup
   в изолированную копию. VPS в рамках этой задачи не опрашивается/не меняется.
3. На копии пройти **все** pending revisions до head. Если VPS отстаёт от этапов 2–4,
   учитывать их backfills/index builds, locks и настройки auth/SSRF/forwarded trust,
   не рассматривать только последнюю миграцию.
4. Оценить размер/время/lock budget: даже additive DDL и CHECK validation блокируют
   конкурирующие операции. Этап 4 дополнительно backfills operation_key, поэтому
   старые writers необходимо остановить до общей цепочки миграций. Не выполнять
   миграции параллельно с E2E mutations. Согласовать окно, сначала миграции, затем app.
5. До запуска новой версии проверить production settings: HTTPS same-origin,
   доверенные forwarded IPs, SSRF policy, cookie/CSRF и начальный admin. Секреты
   не включать в public JS; значения runtime variables/headers/body не писать в
   access/APM/trace captures. Application code не логирует raw query/парсер exceptions.
6. После отдельно одобренного release провести smoke существующих/новых сервисов,
   исторических snapshots/runs, ACL всех ролей, SDL/label, stats и safe document.
   Без durations ожидать null, без исторического AST unavailable, а не fake text.
   Проверить объём: safe text хранится до удаления observations, без automatic TTL;
   максимальный прирост 64 KiB на новый request, плюс индексы/прочие данные журнала.

Безопасное представление всё ещё содержит настоящие имена операций/aliases/полей
и бизнес-структуру; это причина сохранения journal ACL. Placeholders не являются
корректными values для выполнения, код из detail не использовать для replay.
Saved schema SDL по своему read ACL возвращается полностью, включая author
schema defaults; это отдельная категория, а не сохранённые runtime variables.

## Rollback

Отключить новые writers/app перед schema downgrade. Предпочтителен rollback app
с оставлением additive columns. Downgrade revision удаляет labels и safe documents
(потеря новых display данных), CHECK и колонки; observations/fields/runs сохраняет.
Не выполнять его на живой новой версии. Backup и отдельное решение владельца обязательны.

## Этап 6

Независимый аудит полного P0/P1 checklist, frontend end-to-end, проверка operational
logging/trace settings, sizing/retention и прогон миграций на production-like копии.
Функциональные endpoints этапа 5 готовы к ревью, это не объявление VPS готовым к rollout.

Локальная верификация финального этапа 5: PostgreSQL 18.6 / graphql-core 3.3.2 —
501 passed, без skipped, один прежний Starlette/httpx warning. Дополнительно
safe AST/schema/PostgreSQL набор на graphql-core 3.2.14 — 51 passed; контрольный
сценарий открытого PR #22 — 1 passed, 17 deselected. Alembic check, pip check,
Compose config, deploy-script syntax, compileall и diff check пройдены.
Результат GitHub CI указывается в описании PR после его завершения.
