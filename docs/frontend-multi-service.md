# Frontend: контракт этапа 2 v2.0

База — main `bf9cdb2cdbcbdc7d7fd2f4e41b10ea8f910c641a` (PR #23).
Только multi-service и proxy P0; production ещё требует отдельного принятия и выкладки.
Cookie/CSRF, роли и ACL из [frontend-auth.md](frontend-auth.md) сохраняются.
Все пути относительные, same-origin; административный ключ остаётся только на сервере.

| Метод | Путь | Изменение | Доступ |
|---|---|---|---|
| POST | `/api/services` | Обязательный endpointUrl; необязательный description | operator/admin; создателю назначается grant |
| GET | `/api/services` | Массив расширенных ServiceOut | public + выданные grants; admin — все |
| GET | `/api/services/{serviceId}` | Новый endpoint, ServiceOut | public/grant/admin, иначе 404 |
| PATCH | `/api/services/{serviceId}` | name, description, endpointUrl, isPublic | operator с grant / admin |
| POST | `/graphql` | Run ID определяет upstream | operator с grant / admin / CLI key |
| POST | `/api/schemas/import/introspection` | Временно 409 для configured-сервиса | До этапа 3 использовать SDL |

ServiceCreate:

```json
{"key":"orders","name":"Orders","endpointUrl":"https://orders.example.com/graphql","description":"Заказы","isPublic":false}
```

ServiceOut:

```json
{"id":"00000000-0000-0000-0000-000000000001","key":"orders","name":"Orders","description":"Заказы","isPublic":false,"endpointUrl":"https://orders.example.com/graphql","createdAt":"2026-11-15T12:00:00Z","updatedAt":"2026-11-15T12:00:00Z"}
```

endpointUrl возвращается только admin/CLI или operator с grant этого сервиса.
Anonymous/viewer/analyst получают null, в том числе для public-сервиса.
Operator без grant видит public-сервис, но не его адрес. Private UUID без grant — 404.
Список использует EXISTS в одном SQL-запросе, без N+1.
endpointUrl старого сервиса может быть null; UI показывает «Адрес не настроен».
Изменение name/description/isPublic не требует остановки run. Смена endpointUrl
запрещена при любом running run всех snapshots сервиса (409). Тот же URL допустим.
Пустой PATCH и null для name/endpointUrl/isPublic — 422; description:null очищает описание.
key после создания не редактируется. Description публичного сервиса видна всем:
секреты в ней хранить нельзя.

Рекомендуемый порядок интеграции:

1. Подключить login/session, CSRF и grants согласно этапу 1.
2. Добавить endpointUrl/description в форму создания и редактирования сервиса.
3. Обработать nullable/скрытый endpointUrl без подстановки фиктивного значения.
4. Импортировать SDL выбранного сервиса, создать run по snapshotId.
5. Для proxy передавать UUID run в X-Coverage-Run-Id. Он выбирает upstream;
   UUID не заменяет cookie/CSRF или разрешение. Для browser run ID обязателен всегда.
6. Дождаться всех ответов, завершить run, читать существующие coverage/operations/stats.

Изменяющие browser-запросы содержат Origin и X-CSRF-Token, cookie автоматически
передаёт браузер. Upstream Authorization разрешён; все browser cookies и Set-Cookie
отбрасываются, X-Coverage-* и CSRF не пересылаются. Хранить API key в UI запрещено.
Ответы no-store. Новых CORS-настроек и новых pagination-заголовков этап 2 не добавляет;
существующий X-Total-Count журнала сохраняется.

Ошибки: 400 — run ID отсутствует/невалиден; 401/403 — auth/роль/CSRF;
404 — отсутствующая или недоступная запись; 409 — неактивный run/нет endpoint/
активный run при смене адреса/временно недоступная service introspection;
413 — превышен размер; 422 — DTO/URL/DNS не прошёл проверку при сохранении;
503 — DNS/SSRF destination/storage/capacity недоступны перед отправкой;
502/504 — ошибка upstream или сохранения после отправки. Для 502/504 операция
могла выполниться: UI и E2E не должны автоматически повторять mutation.

Run history, request detail, P95 и безопасный AST не входят в этот PR.
Актуальная OpenAPI генерируется FastAPI: `/openapi.json`.
