# Подключение тестов к Test Coverage

Здесь находятся только клиентские адаптеры к backend API:

- `python/` — устанавливаемый HTTP-клиент и pytest-плагин `coverage_test`;
- `playwright/index.cjs` — фабрика fixture и reporter для Node Playwright;
- `playwright/package.json` и lock-файл — зависимости проверки адаптера.

Адаптеры перенесены из QA `1de4be29f0839a2be1e87f0cfe99fe882b3145cf`.
В Python-плагине удалены обращения к внутренним отчётам и панели QA.
Backend не импортирует эти пакеты при обслуживании API; они запускаются
в процессе ваших тестов. Веб-интерфейс, магазин и QA runner остаются в
отдельном [QA-репозитории](https://github.com/QualiMeter/GraphQL-Coverage-Proxy-QA/tree/feat/test-coverage).

## Подготовка

Backend должен иметь миграцию `20261124_test_executions` и активный прогон.
Прогон создаёт ваш сценарий через обычный API; адаптер не управляет его
созданием или закрытием. После завершения всех тестов прогон закрывает
вызывающий сценарий. Контракт — [Test Coverage](../docs/test-coverage.md).

Используются переменные окружения:

| Переменная | Значение |
| --- | --- |
| `COVERAGE_URL` | Базовый URL backend, например `http://127.0.0.1:8000` |
| `COVERAGE_TEST_RUN_ID` | UUID активного прогона |
| `COVERAGE_API_KEY` | Административный ключ для доверенного runner |

Храните ключ в локальном окружении или защищённых переменных CI.
Клиент не следует redirect при отправке метаданных. В GraphQL передаются
`X-Coverage-Run-Id` и `X-Coverage-Test-Execution-Id`; прокси удаляет их перед
отправкой upstream. Сам GraphQL-запрос адаптер автоматически не повторяет.

## pytest

Установите пакет в окружение своего тестового проекта. Путь указывает на
локальный checkout backend:

```sh
python -m pip install -e /path/to/backend/integrations/python
python -m pytest -p coverage_test.pytest_plugin tests
```

На Windows можно использовать путь вида
`C:/Code/GraphQL/GraphQL-Coverage-Proxy-Beckend/integrations/python`.
Перед запуском задайте три переменные из таблицы выше.

```python
import os
import httpx


def test_health(coverage_test):
    response = httpx.post(
        os.environ['COVERAGE_URL'] + '/graphql',
        headers={**coverage_test.headers,
                 'X-Coverage-Api-Key': os.environ['COVERAGE_API_KEY']},
        json={'query': '{ health }'},
    )
    assert response.status_code == 200
    assert response.json()['data']['health'] == 'ok'
```

Запрос и assertion должны соответствовать схеме вашего upstream.
Заголовки привязки добавляйте во все запросы, относящиеся к тесту.
Произвольные HTTP-клиенты плагин не перехватывает. Финальный статус отправляется
после teardown; неудачная проверка при HTTP 200 сохраняется как failed.
Если API не подтвердил доставку результата, pytest завершается с ошибкой.

Для программного клиента без плагина доступен
`from coverage_test import TestExecution`. Методы `start()`,
`finish(status, duration_ms, failure_code=None)` и `read()` работают с тем же
API; `headers` содержит идентификаторы прогона и попытки. `read()` возвращает
серверные метаданные, поля и связанные обращения.

## Node Playwright

Используйте Node.js 22 и свою установку `@playwright/test`. Скопируйте
`playwright/index.cjs` в тестовый проект либо укажите абсолютный путь к нему.
Пример для копии `coverage/index.cjs`:

```javascript
const {test: base, expect} = require('@playwright/test');
const {createCoverageTest} = require('./coverage/index.cjs');
const test = createCoverageTest(base);

test('Health', async ({request, coverage}) => {
  const execution = await coverage.trackRun(process.env.COVERAGE_TEST_RUN_ID);
  const response = await request.post(process.env.COVERAGE_URL + '/graphql', {
    headers: {...execution.headers, 'X-Coverage-Api-Key': process.env.COVERAGE_API_KEY},
    data: {query: '{ health }'},
    maxRedirects: 0,
  });
  expect(response.status()).toBe(200);
  expect((await response.json()).data.health).toBe('ok');
});
```

Для безопасной отправки можно использовать `execution.post(request, {query: '{ health }'})`:
helper добавляет ключ и идентификаторы и всегда задаёт `maxRedirects: 0`.
При ручном вызове `request.post` этот параметр обязателен: Playwright иначе
может повторно отправить пользовательские заголовки на адрес из `Location`.

В `playwright.config.cjs` добавьте reporter:

```javascript
module.exports = {
  retries: 1,
  reporter: [['list'], [require.resolve('./coverage/index.cjs')]],
};
```

Reporter обязателен: он публикует окончательный результат с учётом teardown
и ждёт подтверждения в onEnd. Retry получает отдельную попытку с новым UUID,
сохраняя прежний testId. Параллельные workers не делят один executionId.
Для запросов браузерного приложения настройте передачу идентификаторов через
свой BFF или HTTP-клиент; административный ключ в page-код не передаётся.

## Проверка адаптеров в этом репозитории

```sh
python -m pip install -e '.[test]' -e integrations/python
npm ci --prefix integrations/playwright
python -m pytest tests/test_test_coverage_sdk.py -q
python -m pytest tests/test_test_coverage_runners_postgres.py -q
```

Для последней команды нужны разные `DATABASE_URL` и `TEST_DATABASE_URL`,
как описано в основном README. Проверка создаёт отдельную БД, запускает
настоящий backend и дочерние pytest/Playwright. Намеренно падающие дочерние
assertion и teardown проверяют сохранение failed; родительские тесты должны
пройти. Используется только HTTP-fixture с одним GraphQL-полем, браузер
не требуется. Backend CI устанавливает адаптеры и выполняет эти проверки.

Python SDK повторяет finish при `409/test_requests_in_flight` максимум три раза
с паузами 100/200 мс и бюджетом retry 5 секунд. UUID и итоговый payload сохраняются.
Если подтверждение не получено, runner сообщает ошибку сбора; session cleanup
повторяет известный результат, а `interrupted` использует только для тестов,
которые не успели получить итоговый результат.
