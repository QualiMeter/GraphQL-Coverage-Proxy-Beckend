# Интеграция внешних E2E-тестов с GraphQL Coverage Proxy

Инструкция для внешнего E2E-набора. Контракт текущей реализации:
`POST /graphql → capture_e2e_exchange() → record_observation() → PostgreSQL`.
Run ID выбирает snapshot и endpoint сервиса; browser использует session/CSRF,
CLI — серверный API key. [Пример двух сервисов](multi-service-e2e.md),
[REST API](frontend-api.md), [авторизация](frontend-auth.md).

## 1. Создание, выполнение и завершение test_run

`test_run` — запись прогона, привязанная к одному снимку GraphQL-схемы.
В REST API она называется run. JSON использует camelCase, идентификаторы —
строки UUID, время — ISO 8601 UTC. Один прогон может объединять весь внешний
E2E-набор; отдельная сущность тест-кейса и связь запроса с тест-кейсом отсутствуют.

| Этап | Действие | Результат |
| --- | --- | --- |
| Подготовка сервиса | `GET /api/services`; при необходимости `POST /api/services` с `{"key":"demo-api","name":"Demo GraphQL API","endpointUrl":"https://api.example.com/graphql"}` | Использовать `id` сервиса; повторный key даёт 409 |
| Подготовка схемы | Предпочтительно `POST /api/schemas/import/introspection` с serviceId; при недоступной introspection — `POST /api/schemas/import` с `{"serviceId":"<service UUID>","sdl":"<реальный SDL upstream>"}` | 201; `id` снимка использовать как `snapshotId`; повторный импорт той же схемы возвращает существующий снимок |
| Выбор снимка | `GET /api/schemas?serviceId={serviceId}` | Выбрать версию, соответствующую реальному upstream |
| Создание прогона | `POST /api/runs` с телом ниже | 201; сохранить `id` как runId; `status="running"`, `finishedAt=null` |
| Выполнение | Передать runId всем участникам набора; отправлять GraphQL через прокси с `X-Coverage-Run-Id` | При подключённом анализаторе наблюдение сохраняется до выдачи ответа |
| Ожидание | Дождаться всех GraphQL-ответов и сохранения наблюдений; остановить фоновый polling приложения | Новые запросы больше не поступают |
| Завершение | `POST /api/runs/{runId}/finish`, без тела | 200; объект прогона со `status="completed"` и заполненным `finishedAt` |
| Отчёт | `GET /api/runs/{runId}/coverage` и `/requests` | Сохранить карту и журнал даже при падении E2E |

Тело создания прогона:

```json
{
  "snapshotId": "162ef82d-d806-4f08-b901-e08b5d9228e2",
  "name": "E2E Regression",
  "metadata": {
    "branch": "main"
  }
}
```

UUID здесь иллюстративный: нужен `id` реально импортированного снимка.
`metadata` — произвольный JSON-объект; в нём можно хранить идентификатор внешнего
запуска и commit SHA, но не секреты. Список прогонов:
`GET /api/runs?snapshotId={snapshotId}`.

На одном снимке допускается только один `running`-прогон, иначе создание даёт 409.
Параллельные workers одного набора должны использовать один заранее созданный
runId. Каждый worker не должен создавать и завершать собственный прогон на том
же снимке. Независимые запуски на одном снимке нужно сериализовать.

Оркестратор завершает прогон в `finally`/teardown и при неуспешных тестах.
`completed` означает окончание сбора, а не успешное прохождение тестов:
endpoint `/finish` всегда выставляет `completed`. Хотя модель БД допускает
`failed`, REST-операции установки этого статуса нет. Повторный `/finish` даёт 409,
неизвестный runId — 404, некорректный UUID в REST-пути — 422.
После завершения `record_observation()` отвергает новые записи.

Завершение и запись наблюдения блокируют одну строку прогона. Если `/finish`
опередит ещё не записанный запрос, тот не будет сохранён: прокси вернёт 502 с
предупреждением, что upstream уже мог выполнить запрос. Статус прогона повторно
проверяется перед анализом и под блокировкой строки при записи, с обновлением
ORM-кеша. Прокси дожидается commit перед выдачей исходного ответа upstream;
фоновых задач записи и HTTP API ожидания/flush нет.

## 2. Контракт X-Coverage-Run-Id

Для запроса, который должен попасть в покрытие, E2E-клиент передаёт:

```http
X-Coverage-Run-Id: 57b03e76-29a9-4d9c-a961-1d47ba4f850f
```

Действующий контракт:

- Значение — один UUID из ответа `POST /api/runs`, в форме `8-4-4-4-12`,
  без префикса `Bearer`, URN и фигурных скобок. Допускаются верхний регистр UUID
  и пробелы по краям. Имена HTTP-заголовков регистронезависимы.
  Дублирующиеся строки заголовка (даже с одинаковым UUID) и списки через запятую
  отвергаются.
- Заголовок добавляется к каждому учитываемому GraphQL HTTP-запросу. Он не нужен
  для `/api/...`, загрузки HTML, изображений и обращений к сторонним сервисам.
- runId связывает запрос с прогоном, снимком и endpoint выбранного сервиса; он
  не заменяет авторизацию и не служит идентификатором отдельного тест-кейса.
- Только существующий `running`-прогон может принимать наблюдения.
  При отсутствующем, некорректном, неизвестном или завершённом runId нельзя
  приписывать запрос другому активному прогону или создавать прогон автоматически.
- Прокси читает служебный заголовок для атрибуции и удаляет его перед upstream.

| Ситуация | HTTP | Запрос upstream |
| --- | --- | --- |
| Заголовка нет | 400; только доверенный CLI при явном legacy-режиме получает исходный ответ | Только в legacy CLI, без coverage |
| Некорректный/пустой/неоднозначный UUID | 400 | Нет |
| Анализатор не подключён | 503 | Нет |
| Прогон или его snapshot не найден | 404 | Нет |
| Прогон завершён (`completed` или `failed`) | 409 | Нет |
| БД недоступна при предварительной проверке | 503 | Нет |
| Ошибка сохранения после upstream, включая завершение прогона | 502 | Уже выполнен/мог быть выполнен |

Порядок проверок: синтаксис заголовка → доступность анализатора → прогон и snapshot
→ конфигурация upstream. Поэтому без анализатора корректный UUID получает 503
независимо от наличия прогона в БД. Неизвестный runId никогда не заменяется другим.
Служебный заголовок удаляется даже при `Connection: X-Coverage-Run-Id`.

## 3. Куда направлять GraphQL-запросы

Целевая последовательность трафика:

```text
E2E → реальное веб-приложение → POST <coverage-proxy>/graphql
                                  → реальный GraphQL upstream
                                  ← оригинальный GraphQL-ответ
                           ← ответ для приложения
```

GraphQL URL тестируемого приложения должен быть настроен на адрес прокси
**до первого запроса**, включая запросы при открытии страницы и входе в систему.
Прокси определяет upstream через runId → snapshot → service.endpointUrl.
Сервисы создаются с разными endpointUrl, глобальный адрес при переключении не меняется.
TARGET_GRAPHQL_URL применяется только в явно включённом временном legacy-режиме.

Прямые обращения приложения к upstream обходят наблюдение. Если запросы выполняет
сервер приложения (SSR/BFF), его GraphQL-клиент тоже должен использовать прокси
и передавать runId: маршрутизация браузера Playwright такие запросы не охватывает.
Фронтенд карты обращается к `/api/...`; он не создаёт наблюдения самостоятельно.

Для браузера нужны рабочие CORS и OPTIONS на фактическом адресе прокси.
При запросе с заголовком браузер должен иметь право передать `X-Coverage-Run-Id`
и используемые приложением заголовки, включая `Authorization`.
Текущий FastAPI разрешает все методы и заголовки для origin из `CORS_ORIGINS`
(по умолчанию список пуст), но `allow_credentials` не включён.
Cross-origin cookies требуют отдельного решения; для демо с cookie-auth удобен
один origin с заранее настроенной маршрутизацией. Этот документ её не меняет.

## 4. Authorization и прозрачная пересылка

Прокси обязан передать исходный `Authorization` приложения upstream без замены
его на runId или токен Coverage API. Если клиент не передал `Authorization`,
прокси не должен придумывать его. Аутентификация, cookies и права пользователя
остаются частью реального приложения.

Прокси реализует пересылку исходных байтов тела и применимых заголовков, включая
`Authorization`. Cookie/Set-Cookie сохраняются только для доверенных API-key
CLI/E2E без Origin и Sec-Fetch-*; browser context блокирует их целиком. Для каждого
сервиса/run использовать отдельный cookie jar и не общий browser storage. Исключаются `X-Coverage-Run-Id`, hop-by-hop заголовки
и заголовки, перечисленные в `Connection`; `Host` и `Content-Length` формируются
заново. Это также означает, что некорректный клиентский `Connection: Authorization`
приведёт к исключению `Authorization` как hop-by-hop заголовка.

При успешной записи наблюдения HTTP-статус и тело upstream, включая GraphQL
`errors` и HTTP 4xx/5xx доходят до приложения без изменений. Upstream Set-Cookie
передаются только доверенному API-key CLI/E2E вне browser context.
Входящие `X-Forwarded-*`, `Forwarded` и `X-Real-IP` удаляются, клиент не задаёт
доверенные forwarding-метаданные. Анализатор читает копию данных в памяти.
Исходные байты ответа получаются через `aiter_raw()` и сохраняют сжатие.
Для анализа поддержаны identity, gzip и zlib-wrapped deflate: предел и входной
копии, и распакованного результата — 1 МиБ. Неизвестные/составные кодирования,
повреждённые, усечённые, соединённые потоки и превышение предела дают
`decoded_response_body=None`; не-JSON/слишком глубокий JSON тоже даёт неизвестные
признаки ответа. Распаковка ограничена во время выполнения, а не после выделения
неограниченного буфера. Предел касается анализа: исходные HTTP-тела, как и раньше,
целиком буферизуются для прозрачной передачи.
Raw query, runtime variables, `Authorization` и cookies не сохраняются.
Допустимы SHA-256 текста (`query_hash`), [masked AST](frontend-final-features.md)
и [очищенные error metadata](graphql-error-details.md) в защищённом detail.

## 5. Proxy → GraphQL Analyzer → record_observation()

Это внутренняя Python-интеграция, а не публичный REST endpoint для загрузки
«готового покрытия».

1. Proxy проверяет UUID, наличие анализатора, `running`-прогон и его snapshot.
2. Отправляет исходный запрос upstream и получает оригинальный ответ.
3. Вызывает `await capture_e2e_exchange(...)` с исходным телом, HTTP-статусом,
   длительностью до получения всего upstream-ответа (мс, без анализа/записи)
   и отдельной декодированной копией ответа или `None`.
4. Адаптер формирует `GraphQLQueryInput` из одиночного JSON-запроса и вызывает
   `await analyzer(gql_input, snapshot.schema_sdl)` со схемой именно этого прогона.
5. Адаптер вызывает `await record_observation(...)`. Только после commit прокси
   возвращает исходный ответ. Поля разрешаются исключительно внутри snapshot прогона.

### Подключение настоящего анализатора

Интерфейс из `app.e2e_capture`:

```python
Analyzer = Callable[[GraphQLQueryInput, str], Awaitable[AnalysisResult]]
```

Приложение явно предоставляет callback через `app.state.coverage_analyzer`.
`app.main` устанавливает `analyze_e2e_query()` из `app.analyzer_adapter`: он
преобразует SDL снимка в `GraphQLSchema` (bounded LRU-кеш на 32 схемы) и вызывает
`app.query_analyzer.analyze_query()` в отдельном worker thread. Runtime mock или
fallback-результаты покрытия отсутствуют. Ошибка компиляции повреждённого SDL
сохраняется как `internal_error`, без фиктивных полей. В тестах callback может
подменяться для изоляции проверки БД или сетевого поведения.

### Ошибки анализа и сохранения

- Неподдерживаемая форма запроса (например, batching или persisted query без
  текста) записывается как `unsupported`, без полей; upstream получает исходное тело.
- Исключение анализатора записывается как `internal_error/analyzer_failure`, без
  полей и текста исключения. При сохранённом наблюдении оригинальный ответ
  upstream возвращается клиенту. Это запись ошибки анализа, а не успешное покрытие;
  статус виден в `/api/runs/{runId}/requests`.
- Ошибка БД/commit, недопустимые ссылки на поля или завершение прогона после
  предварительной проверки дают 502 с фиксированным сообщением:
  `Coverage observation could not be saved; upstream may have executed the request. Do not retry automatically.`
  Исходный ответ в этом случае не отправляется, запись откатывается при закрытии
  request-scoped сессии. Подтверждение commit при обрыве соединения может быть
  неопределённым: надо сверить журнал и состояние upstream. Прокси не повторяет
  запрос. Особенно для mutation клиенту нельзя автоматически повторять его:
  upstream уже мог изменить данные. Между upstream и PostgreSQL нет общей транзакции.
- Сетевые ошибки upstream по-прежнему дают 502, таймауты — 504; для незавершённого
  обмена наблюдение не создаётся. Исключения и содержимое тел в ошибки/логи
  интеграции не выводятся.

Существующие структуры из `app/contracts.py`:

| Структура | Поля |
| --- | --- |
| `FieldReference` | `type_name: str`, `field_name: str` |
| `GraphQLQueryInput` | `query: str`, `operation_name: str \| None = None`, `variables: dict = {}` через default factory |
| `AnalysisResult` | `status: str`, `operation_type: str \| None`, `operation_name: str \| None`, `fields: tuple[FieldReference, ...] = ()`, `error_code: str \| None`; необязательные значения по умолчанию `None` |
| `RequestObservationInput` | `run_id: UUID`, `analysis: AnalysisResult`, `http_method: str`, `http_status: int \| None`, `response_has_data: bool \| None`, `graphql_has_errors: bool \| None`, `duration_ms: int \| None = None`, `query_hash: str \| None = None`, `safe_document: SafeDocument`, `graphql_errors: ErrorSummary` (default factories) |

Сервис принимает статусы анализа `success`, `parse_error`, `validation_error`,
`unsupported`, `internal_error`. Неуспешный анализ обязан иметь пустой `fields`;
иначе запись отклоняется с `InvalidReferenceError`. При известном активном прогоне
неуспешный анализ можно сохранить в журнал без добавления покрытия.

`record_observation()` сам не разбирает AST или ответ upstream. Он проверяет
прогон, разрешает пары `(type_name, field_name)` только внутри его снимка,
отвергает отсутствующие поля и атомарно сохраняет `ObservedRequest` и
`RequestFieldUsage`. Возвращается UUID наблюдения. Для неизвестного прогона
возникает `NotFoundError`, для завершённого — `ConflictError`. Предварительная
проверка отображает их в 404/409; после выполнения upstream применяется 502 выше.

## 6. Покрытие и чтение результатов

Формула покрытия, OBJECT denominator, hits и DTO графа/аналитики описаны в
[REST-контракте](frontend-api.md). Журнал, pagination и request detail — в
[контракте истории](frontend-history-requests.md). Его чтение требует analyst/operator
с grant либо admin/CLI даже для публичного сервиса. AST и GraphQL error details
доступны только в защищённом detail; список обращений их не содержит.

## 7. Минимальное подключение существующего Playwright-набора

Это выдержки для внешнего E2E-репозитория, не новый Playwright-проект или тест.
Предварительно нужны подключённый настоящий анализатор (раздел 5), реальный SDL,
настоящий upstream и приложение, уже настроенное обращаться к прокси.

Минимальная fixture добавляет заголовок только запросам к точному URL прокси.
Существующие тесты должны импортировать её `test` вместо базового `test`;
при наличии своих fixtures нужно встроить этот шаг в их настройку context.
Маршрут устанавливается до навигации, другие GraphQL-маршруты с подменой ответов
для этого набора должны быть отключены.

```ts
import { test as base, expect } from '@playwright/test';

export const test = base.extend({
  context: async ({ context }, use) => {
    const runId = process.env.COVERAGE_RUN_ID;
    const proxyUrl = process.env.COVERAGE_GRAPHQL_URL;
    if (!runId || !proxyUrl) {
      throw new Error('COVERAGE_RUN_ID and COVERAGE_GRAPHQL_URL are required');
    }
    const endpoint = new URL(proxyUrl);
    await context.route(url => url.href === endpoint.href, async route => {
      const request = route.request();
      await route.continue({
        headers: {
          ...await request.allHeaders(),
          'x-coverage-run-id': runId,
        },
      });
    });
    await use(context);
  },
});

test.use({ serviceWorkers: 'block' });
export { expect };
```

`route.continue()` отправляет запрос в сеть. Здесь не меняются URL, метод, тело,
исходный `Authorization` и ответ; не используются `fulfill`, HAR или локальные
GraphQL-ответы. Этот browser-пример требует явной авторизации Coverage через сессию/CSRF
либо доверенного серверного адаптера: административный ключ в browser внедрять нельзя.
Browser Cookie к upstream и upstream Set-Cookie блокируются; cookie-auth upstream
проверяется серверным E2E-клиентом с отдельным jar на сервис. Точный matcher требует,
чтобы приложение использовало тот же URL, включая query string, что и
`COVERAGE_GRAPHQL_URL`. Redirect на этом endpoint для демо не нужен.
Service Workers блокируются, чтобы они не обходили перехват context.
Пример опирается на официальные API Playwright:
[context.route](https://playwright.dev/docs/api/class-browsercontext#browser-context-route),
[route.continue](https://playwright.dev/docs/api/class-route#route-continue)
и [переопределение fixtures](https://playwright.dev/docs/test-fixtures#overriding-fixtures).

Один внешний оркестратор создаёт общий прогон до запуска workers и завершает его
после выхода Playwright. Например, следующий Bash-фрагмент выполняется в уже
существующем E2E-репозитории с `curl`, `jq` и установленным Playwright:

```bash
set -euo pipefail
: "${COVERAGE_API_URL:?REST API base URL is required}"
: "${COVERAGE_SNAPSHOT_ID:?Imported snapshot UUID is required}"
: "${COVERAGE_GRAPHQL_URL:?Proxy GraphQL URL is required}"
: "${COVERAGE_API_KEY:?Server-side Coverage API key is required}"

payload=$(jq -n --arg snapshotId "$COVERAGE_SNAPSHOT_ID" \
  '{snapshotId: $snapshotId, name: "External E2E", metadata: {}}')
run=$(curl --fail-with-body --silent --show-error \
  -H "X-Coverage-Api-Key: $COVERAGE_API_KEY" \
  -H 'Content-Type: application/json' --data "$payload" \
  "$COVERAGE_API_URL/api/runs")
COVERAGE_RUN_ID=$(jq -er '.id' <<< "$run")
export COVERAGE_RUN_ID COVERAGE_GRAPHQL_URL

finish_and_report() {
  test_status=$?
  trap - EXIT
  curl --fail-with-body --silent --show-error -X POST \
    -H "X-Coverage-Api-Key: $COVERAGE_API_KEY" \
    "$COVERAGE_API_URL/api/runs/$COVERAGE_RUN_ID/finish" || exit 1
  curl --fail-with-body --silent --show-error \
    -H "X-Coverage-Api-Key: $COVERAGE_API_KEY" \
    "$COVERAGE_API_URL/api/runs/$COVERAGE_RUN_ID/coverage" || exit 1
  exit "$test_status"
}
trap finish_and_report EXIT
npx playwright test
```

`COVERAGE_API_URL` — адрес API без завершающего `/`, например
`http://localhost:8000`; `COVERAGE_GRAPHQL_URL` — полный адрес `/graphql`.
Переменные `COVERAGE_*` — соглашение примера во внешнем наборе, а не новые
настройки бэкенда. Приложению нужен его собственный механизм настройки GraphQL URL.
Аутентификация приложения выполняется существующим способом тестового набора.

Тесты должны дождаться GraphQL-ответов, а teardown — остановить фоновые запросы
до выхода процесса. Оркестратор предполагает гарантию записи до ответа из
раздела 1. Отчёт выводится и при падении тестов, исходный ненулевой exit code
Playwright сохраняется, а ошибка завершения/получения отчёта тоже даёт ошибку.
EXIT trap не спасает от SIGKILL или потери runner: незавершённый run потребует
явного `/finish` оператором. Удалять или переиспользовать чужой running-прогон нельзя.

## 8. Ограничения и диагностика

Нужны настоящий upstream, приложение с GraphQL URL на proxy, PostgreSQL и
внешний набор с авторизацией Coverage. Прямые SSR/BFF-запросы к upstream
обходят наблюдение; их клиент тоже должен использовать proxy и run ID.

Поддержан HTTP POST с буферизацией: request 1 MiB, response 8 MiB,
анализ декодированной копии 1 MiB, upstream exchange 30 с, до 16 слотов/worker.
WebSocket, streaming, batching и persisted query без текста не анализируются.
Анализатор раскрывает именованные и inline-фрагменты, применяет условия типов
для интерфейсов и union и вычисляет `@skip`/`@include` с переменными запроса.
Координата берётся из типа, на котором выбрано поле. Например, `id` во
фрагменте `on Node` считается как `Node.id` и даёт максимум один hit за запрос
даже при повторном раскрытии. Для union учитываются выбранные поля всех
возможных веток, независимо от того, какие типы пришли в ответе.

Документ ограничен 10 000 токенов. Обход ограничен 64 уровнями и 20 000
посещениями полей или фрагментов. При превышении лимита анализ не возвращает
частичных результатов. Subscriptions и неподдерживаемые директивы дают
`unsupported` без полей.
[SSRF, DNS pinning и ограничения](multi-service-security.md).

Сначала проверьте настоящий пользовательский результат и строки журнала,
затем coverage/hits. Заполненные вручную наблюдения не доказывают интеграцию.
`/health` проверяет процесс; `/api/services` дополнительно проверяет БД.
Неизвестный или недоступный родитель в списках схем/прогонов даёт 404.
Run ID не заменяет авторизацию. Автоматического закрытия зависших runs,
связи с test case и REST-операции установки failed нет.
