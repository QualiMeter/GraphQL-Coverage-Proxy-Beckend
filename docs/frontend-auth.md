# Авторизация frontend и управление доступом

Действующий контракт авторизации. Остальные API-контракты собраны в
[указателе документации](README.md); наличие кода не подтверждает его выкладку.

Frontend и API используют один origin `https://graphql.rebyte.fun`.
Все пути относительные. `X-Coverage-Api-Key` остаётся только в доверенных CLI/CI;
не помещать его в Vite, React, localStorage или публичный JavaScript.

## Модель доступа

| Действие | Anonymous | Viewer | Analyst | Operator | Admin / CLI key |
|---|---|---|---|---|---|
| Публичные сервисы, схемы, история, coverage, operations, stats | Да | Да | Да | Да | Да |
| Приватные сервисы и их агрегаты | Нет | Grant | Grant | Grant | Все |
| Журнал requests | Нет | Нет | Grant | Grant | Все |
| Создать сервис | Нет | Нет | Нет | Да, grant автоматически | Да |
| Изменить сервис, импортировать схему, создать/завершить run, proxy | Нет | Нет | Нет | Grant | Все |
| Пользователи и grants | Нет | Нет | Нет | Нет | Да |

Grant — разрешение на конкретный сервис, назначенное admin.
Публичность разрешает только чтение агрегатов, а не журнал или изменения.
Знание UUID не даёт прав. Проверяется вся цепочка run → snapshot → service.
Смена роли, пароля или активности отзывает все сессии пользователя.
Отзыв grant действует при следующем запросе без повторного входа.
Произвольный `RunOut.metadata` возвращается только admin/CLI; остальным `{}`.

## Контракт endpoints

| Метод и путь | Тело / результат | Доступ |
|---|---|---|
| `POST /api/auth/login` | `{username,password}` → `SessionOut`, Set-Cookie | Точный Origin, HTTPS, ограничение попыток |
| `GET /api/auth/session` | `SessionOut`; анонимно все поля null | Открыт, без кеширования |
| `POST /api/auth/logout` | 204; отзыв текущей сессии и удаление cookie | Origin + CSRF для действующей сессии |
| `GET /api/auth/users` | `UserOut[]` | Admin |
| `POST /api/auth/users` | `{username,password,role?}` → `UserOut`, 201 | Admin |
| `PATCH /api/auth/users/{userId}` | `{role?,isActive?,password?}` → `UserOut` | Admin; непустые ненулевые изменения |
| `PUT /api/auth/users/{userId}/services/{serviceId}` | 204, идемпотентный grant | Admin |
| `DELETE /api/auth/users/{userId}/services/{serviceId}` | 204, идемпотентный отзыв grant | Admin |

`role`: `viewer | analyst | operator | admin`; по умолчанию viewer при создании.
Имена — lowercase slug `[a-z][a-z0-9_.-]*`, до 100 символов.
Новый пароль — 12–128 символов. Нет публичной регистрации, HTTP bootstrap,
восстановления пароля по email или OAuth. Admin может сменить пароль через PATCH.
Нельзя отключить или понизить последнего активного admin.
API key имеет полномочия admin; explicit key в запросе имеет приоритет над cookie.
Неверный/дублированный key не переключается на cookie.

```ts
type Role = 'viewer' | 'analyst' | 'operator' | 'admin';
type UserOut = { id: string; username: string; role: Role; isActive: boolean };
type SessionOut = {
  user: UserOut | null;
  csrfToken: string | null;
  expiresAt: string | null;
};
```

Пример ответа login/session:

```json
{
  "user": {
    "id": "ca987c43-d56e-478e-9a34-53ca397ed350",
    "username": "operator",
    "role": "operator",
    "isActive": true
  },
  "csrfToken": "<opaque csrf token, keep in memory>",
  "expiresAt": "2026-11-16T00:00:00Z"
}
```

Cookie: `__Host-coverage_session`, `Secure; HttpOnly; SameSite=Lax; Path=/`, без Domain,
срок 12 часов без автоматического продления. Значение — 32 случайных байта;
БД хранит только SHA-256. На каждом login создаётся новый токен, предыдущий
предоставленный session token отзывается. CSRF — HMAC, связанный с сессией;
в cookie или localStorage его сохранять не нужно. GET session восстанавливает
его после перезагрузки страницы. UserOut никогда не содержит passwordHash.

Login требует точного `Origin`, включая схему и порт; нет fallback на Host,
Referer или присланный `X-Forwarded-Host`. Unsafe-запросы с cookie дополнительно
требуют `X-CSRF-Token`. SameSite не заменяет эту проверку. CLI key не требует CSRF.
При browser session proxy **не пересылает никакие Cookie upstream** и
**не отдаёт никакие upstream Set-Cookie браузеру**, включая неизвестные имена.
Это исключает утечку cookie других приложений общего origin и перенос cookies
между сервисами. Обычные явные Authorization и исходное тело сохраняются.
CLI/E2E с валидным API key без Origin/Sec-Fetch-* сохраняет Cookie/Set-Cookie
passthrough; зарезервированные Coverage cookies и внутренние заголовки удаляются
в любом режиме. Запрос с Origin либо любым Sec-Fetch-* считается browser context
для этой политики даже при API key; ни query-параметр, ни заголовок клиента не
включают исключение. Browser cookie-auth к стороннему upstream таким способом
не поддерживается: использовать явный upstream Authorization либо доверенный
серверный E2E-клиент с отдельным cookie jar на сервис/run. Глобального cookie
allowlist/jar нет. Browser `/graphql` требует run ID и grant. CLI без run ID разрешён только
при явном `COVERAGE_LEGACY_PROXY=true`; configured-сервисы используют свой endpoint.

Все `/api/*` и `/graphql` ответы имеют `Cache-Control: no-store` и Vary по
Cookie/API key. Тела auth ограничены 16 КиБ, в том числе chunked.
Ошибки валидации auth не возвращают input/ctx с присланными паролями.

## Рекомендуемый порядок интеграции

1. При старте получить `/api/auth/session`; хранить SessionOut в памяти.
2. Сделать форму login и индикатор пользователя; `password` очищать после запроса.
3. Отправлять cookie через `credentials: 'same-origin'` и CSRF на unsafe-запросах.
4. Показывать действия по роли. 403/404 от сервера остаются окончательной проверкой.
5. Admin создаёт пользователей и назначает grants; operator создаёт сервисы/схемы/runs.
6. Использовать [действующие DTO](README.md); не подменять ошибки mock-данными.

```ts
let session: SessionOut = { user: null, csrfToken: null, expiresAt: null };

async function refreshSession() {
  const response = await fetch('/api/auth/session', { credentials: 'same-origin' });
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  session = await response.json();
}

async function login(username: string, password: string) {
  const response = await fetch('/api/auth/login', {
    method: 'POST', credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ username, password })
  });
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  session = await response.json();
}

async function apiWrite(path: string, method: string, body?: unknown) {
  const response = await fetch(path, {
    method, credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': session.csrfToken ?? '' },
    body: body === undefined ? undefined : JSON.stringify(body)
  });
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  return response.status === 204 ? undefined : response.json();
}
```

Origin устанавливает браузер; не пытаться установить его через fetch вручную.
Для локального UI использовать Vite proxy к локальному API и настроить
`COVERAGE_DEV_MODE=true`, `COVERAGE_PUBLIC_ORIGIN=http://localhost:5173`.
CORS для cookie-сессий не включается: same-origin достаточно. По умолчанию
список CORS пуст, `allow_credentials=false`. Для существующих отдельных
публичных клиентов можно явно задать CORS_ORIGINS; `X-Total-Count` включён
в Expose-Headers и возвращает total совпадений в истории и журнале до pagination.

## Ошибки

| HTTP | Значение |
|---|---|
| 401 | Нужна действующая сессия/API key; неверный login возвращает одинаковое сообщение |
| 403 | Недостаточная роль, неверные Origin/CSRF или HTTP в production |
| 404 | Нет записи либо нет grant на закрытые данные/управление |
| 409 | Занято имя пользователя либо попытка отключить последнего admin |
| 413 | Auth-тело больше 16 КиБ |
| 422 | Неверный DTO; auth validation не содержит присланные значения |
| 429 | Лимит login; Retry-After: 900 |
| 503 | Неверная конфигурация origin; legacy protected request без key/session при ненастроенном API key |

Login ограничен 10 попытками на username и 30 на адрес соединения за 15 минут,
включая успешные попытки. Счётчики атомарны в PostgreSQL и общие для workers.
X-Forwarded-For сам endpoint не разбирает; адрес берётся из ASGI peer.
Scrypt: N=2^17, r=8, p=1, случайная соль 16 байт. Одновременно до двух KDF на worker
(около 256 МиБ памяти на две операции); учитывать число workers.
Нужен проверенный reverse proxy с корректным HTTPS scheme в ASGI.
Доверять proxy headers следует только известным адресам reverse proxy через
настройку Uvicorn `FORWARDED_ALLOW_IPS`; публичный ввод доверенным не становится.
Production Compose требует точные IP непосредственного peer после Docker NAT,
запускает `app.server` и фиксирует dev mode=false. Wildcard и сети отвергаются.
Nginx перезаписывает X-Forwarded-Proto через реальный `$scheme`, а XFF — `$remote_addr`.
Конфигурация reverse proxy: [deploy/README.md](../deploy/README.md).
Исторический порядок внедрения auth: [PR23-RELEASE.md](../deploy/PR23-RELEASE.md).

## Bootstrap, обслуживание и миграция

Применить Alembic локально в согласованном окружении, затем создать первого admin:

```bash
alembic upgrade head
python -m app.auth_cli bootstrap-admin initialadmin
```

Пароль вводится дважды через getpass, не через argv/env/URL. Bootstrap доступен
только CLI, проверяет отсутствие любого admin и сериализует конкурирующие запуски
PostgreSQL advisory lock. Пароль не выводится. После создания admin использовать
защищённый `/api/auth/users`. Периодически удалять истёкшие служебные записи:

```bash
python -m app.auth_cli cleanup
```

Миграция `20261115_auth` после `20261114_visibility` добавляет auth_users,
auth_sessions, auth_service_grants, auth_login_throttles и индексы. Не меняет
семь прежних таблиц, публичность, snapshots, runs и observations. Down удаляет
учётные записи/сессии/grants; исторические данные сохраняются. Для rollback
сначала завершить процессы новой версии. Не выполнять downgrade без резервной
копии auth-данных, если уже заведены реальные аккаунты.

Основание параметров и защиты: [OWASP Password Storage](https://cheatsheetseries.owasp.org/cheatsheets/Password_Storage_Cheat_Sheet.html),
[Session Management](https://cheatsheetseries.owasp.org/cheatsheets/Session_Management_Cheat_Sheet.html),
[CSRF Prevention](https://cheatsheetseries.owasp.org/cheatsheets/Cross-Site_Request_Forgery_Prevention_Cheat_Sheet.html).
Используется доступный в Python stdlib scrypt; дополнительные auth-сервисы и
production-секреты не нужны. Bootstrap отказывается принимать пароль без TTY.
