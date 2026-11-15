# Этап 1 v2.0 — доступ и контракты

Правки по ревью PR #23 и актуальные проверки: [pr23-review-report.md](pr23-review-report.md).
Ниже сохранён исходный отчёт первого этапа до этих правок.


## Реализовано

Серверные PostgreSQL-сессии, Secure/HttpOnly/SameSite cookie, CSRF и точный Origin,
роли viewer/analyst/operator/admin и grants на сервисы. Cookie и CSRF не уходят
upstream; upstream не может заменить нашу cookie. Public aggregates сохраняются;
журнал и изменения требуют роли/grant. Ключ CLI/E2E работает как прежде.

Bootstrap первого admin — только интерактивная CLI с getpass и PostgreSQL lock.
Нет самостоятельной регистрации. Реализованы отзыв сессий, управление пользователями,
сброс пароля администратором, защита последнего admin, атомарный login throttle,
лимит auth body, отсутствие credential input в ошибках и запрет кеширования API.

## Endpoints

Новые: POST `/api/auth/login`, GET `/api/auth/session`, POST `/api/auth/logout`,
GET/POST `/api/auth/users`, PATCH `/api/auth/users/{userId}`,
PUT/DELETE `/api/auth/users/{userId}/services/{serviceId}`.

Существующие services, schemas, runs, coverage, operations, stats, requests и proxy
используют сессию/роль/grant. `/graphql` из браузера требует run ID; legacy CLI
passthrough пока сохранён. DTO агрегатов и семантика Coverage Engine не изменены.
Изменён текст 401 для защищённых маршрутов: `Authentication required`.
OpenAPI описывает API key **или** cookie; CSRF описан как заголовок.

## Миграция

`20261115_auth` после `20261114_visibility`: auth_users, auth_sessions,
auth_service_grants, auth_login_throttles с FK/CHECK/UNIQUE и индексами.
Прежние core-таблицы и исторические данные не меняются. Downgrade удаляет auth-данные.
Проверены upgrade/downgrade/upgrade с существующими service/snapshot/run/observation.

## Точные локальные результаты

Окружение: Python 3.12.3, PostgreSQL 18.6, FastAPI 0.163.0, Starlette 1.10.0,
SQLAlchemy 2.1.8, Pydantic 2.14.3, pytest 9.2.0. Выделенный временный кластер,
порт 55432; production не использовался.

| Проверка | Результат |
|---|---|
| Исходная main: `pytest -q`, TEST_DATABASE_URL задан | 192 passed, 1 warning in 10.94s |
| Финальный полный набор: `pytest -q`, TEST_DATABASE_URL задан | 223 passed, 1 warning in 33.29s |
| Контрольный тест PR #22 поверх ветки | 1 passed in 1.41s |
| `alembic upgrade head` | Успешно |
| `alembic check` | No new upgrade operations detected |
| Migration-тест на отдельной временной БД | Успешно, включён в полный набор |
| `pip check` | No broken requirements found |
| `docker compose config --quiet` | Exit 0 |
| `bash -n deploy/vps/graphql-coverage-deploy.sh` | Exit 0; скрипт не выполнялся |
| `git diff --check` | Exit 0 |

Предупреждение — StarletteDeprecationWarning о httpx в TestClient. Проверки не
отключались; существующее падение Swagger из описания PR #22 не воспроизвелось.
Старые security assertions обновлены для нового реального контракта
(API key OR cookie и общий текст 401), без исключения тестов.
Дополнительный тест обнаружил и закрыл обход auth-защит под ASGI root_path:
тела ограничиваются, пароли не отражаются в validation response, no-store сохраняется.

PR #22 не cherry-pick и не дублируется в этом PR. Его файл взят из commit
`638d14838ee54e5ef2825179ba3d16d9adea22db` во временный каталог; команда:

```bash
PYTHONPATH=tests pytest -c pyproject.toml -q \
  /tmp/coverage-v2-pr22/test_proxy_e2e_postgres.py::test_control_scenario_coverage_through_proxy
```

Первый запуск внешнего файла без `-c` не загрузил asyncio-конфигурацию проекта;
повтор с явным `-c pyproject.toml` завершился успешно. Это ошибка команды проверки,
не отключение падающего теста.

## Оставшийся объём и риски

Это только этап 1. Multi-service/SSRF, introspection выбранного сервиса, расширенные
схемы/история/журнал/detail, analysisStatusCounts/P95 и безопасный реальный AST
ещё не реализованы. Безопасный документ **включён в P1** последним уточнением
заказчика; его нельзя подменять синтетическим запросом. P2 не начинался.

До выкладки нужны миграция auth, создание admin, grants и проверка HTTPS scheme
от доверенного reverse proxy. Scrypt использует около 128 МиБ на операцию,
не более двух одновременно на worker; учитывать суммарную память. Login throttle
применяется по ASGI peer и username; правильно настроить доверенные proxy IP.
CLI `cleanup` удаляет истёкшие служебные записи; расписание эксплуатации не создавалось.
Тест миграций требует CREATEDB только на тестовом сервере.

Новая миграция не переписывает исторические данные. Production, секреты и VPS
не изменялись; deployment script проверялся только на синтаксис. PR не сливается
автоматически. Полная P0/P1 backend-часть пока не завершена.

Контракт и инструкция frontend: [frontend-auth.md](frontend-auth.md).
План остальных PR: [v2-implementation-plan.md](v2-implementation-plan.md).
