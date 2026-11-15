# Документация GraphQL Coverage Backend

Контракты описывают код текущей ветки. Наличие документа или успешного CI
не подтверждает, что эта версия развёрнута в production.

## Действующие контракты

| Документ | Содержание |
|---|---|
| [REST API](frontend-api.md) | Подключение, маршруты, coverage, операции и статистика |
| [Авторизация](frontend-auth.md) | Роли, grants, session/CSRF, bootstrap и обслуживание |
| [Сервисы](frontend-multi-service.md) | Endpoint CRUD, видимость конфигурации и маршрутизация |
| [Introspection API](frontend-introspection.md) | DTO, HTTP/code, ошибки и ручной SDL fallback |
| [История и журнал](frontend-history-requests.md) | Фильтры, total/pagination, selectedFields и ACL detail |
| [Статистика, AST и metadata](frontend-final-features.md) | P95, маскирование документа, SDL, labels и граф |
| [GraphQL error details](graphql-error-details.md) | Очищенные count/code/path/locations/safeMessage только в detail |

Полные модели и параметры генерируются в `/openapi.json` из
[api_models.py](../app/api_models.py), [auth_contracts.py](../app/auth_contracts.py)
и [journal.py](../app/journal.py). Дополнительные поля detail из двух последних
контрактов применяются вместе с общим контрактом журнала.

## Архитектура и эксплуатация

- [E2E-интеграция](e2e-integration.md): жизненный цикл run, Python capture и внешний runner.
- [Два upstream](multi-service-e2e.md): пример независимых сервисов и прогонов.
- [SSRF и outbound transport](multi-service-security.md): URL/DNS/IP/TLS, лимиты и отсутствие retries.
- [Устройство introspection](service-introspection.md): раздельные транзакции,
  reauthorization, dedup/savepoint и предложение upstream credentials.
- [Deployment](../deploy/README.md) и [автодеплой](../deploy/AUTODEPLOY.md).

## Исторические решения и проверки

Эти материалы сохраняют исходные результаты и решения. Их statements о
«следующем этапе», открытых PR и состоянии production относятся к указанной базе.
Для текущего API используйте контракты выше.

| Документ | Почему сохранён |
|---|---|
| [Первый день Coverage Engine](backend-3-day1.md) | Исходный дефект OBJECT denominator и план regression-проверок |
| [План v2](v2-implementation-plan.md) | Исходное ТЗ, приоритеты и принятые решения |
| [Этап 1](v2-stage1-report.md) | Введение auth и миграция исторических данных |
| [Ревью auth](pr23-review-report.md) | Cookie isolation и proxy trust после Docker NAT |
| [Этап 2](v2-stage2-report.md) | Multi-service/SSRF и переход с legacy routing |
| [Этап 3](v2-stage3-report.md) | Introspection, rollback и владение DB sessions |
| [Аудит этапа 6](stage6-audit-report.md) | Pool/metadata/operation-name/DNS regressions и downgrade lock |
| [Reliability](reliability-fixes-report.md) | Deployment retry, success journal и CPU cancellation |
| [Диагностика](error-handling-report.md) | Allowlisted events, silent logging failure и завершение с blocked sink |

Исторические release procedures:
[auth](../deploy/PR23-RELEASE.md), [endpoints](../deploy/STAGE2-RELEASE.md),
[safe AST](../deploy/STAGE5-RELEASE.md),
[root-owned deploy script](../deploy/RELIABILITY-RELEASE.md).
Они содержат разные предварительные условия выпуска; применять их механически
как общий порядок для нового release нельзя.

[Полный аудит перед ревью PR #31](repository-audit.md).
