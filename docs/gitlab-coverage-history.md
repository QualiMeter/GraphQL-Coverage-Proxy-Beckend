# История покрытия в GitLab CI

## Список прогонов

`GET /api/runs` принимает один из параметров `snapshotId` или `serviceId`.
Для сравнения сначала выберите один снимок, ветку и, если оно указано,
окружение. Пример для истории завершённых прогонов из QA:

```text
GET /api/runs?snapshotId=<UUID>&status=completed&branch=main&environment=qa%2Fstaging&sortBy=startedAt&sortOrder=desc&limit=50
```

Доступны точные фильтры `branch`, `commitSha` (полный SHA в нижнем регистре),
`environment`, `pipelineId`, `pipelineStatus`, `jobId`, `jobName` и `jobStatus`.
Условия объединяются через AND. Если заданы несколько фильтров job, им должна
соответствовать одна и та же job. Период задаётся как `from` (включительно) и
`to` (исключительно) по времени начала прогона. `X-Total-Count` содержит число
прогонов после фильтрации, до применения `limit` и `offset`.

`sortBy` принимает `startedAt`, `branch`, `commitSha`, `environment`,
`pipelineId`, `pipelineStatus`, `jobId` и `jobName`. Для сортировки по job
берётся минимальный ID или имя среди подходящих jobs каждого прогона.
Отсутствующие значения идут в конце при любом направлении. При равных ключах
прогоны упорядочиваются по UUID в том же направлении.

В объекте `ci` доступны `projectId`, `branch`, `commitSha`, `environment`,
`pipelineId`, `pipelineUrl`, `pipelineStatus`, `jobsSyncedAt` и `jobsFinalized`.
Открывайте `pipelineUrl` для перехода в GitLab. `ci: null` означает, что
подтверждённой привязки пока нет или у пользователя нет доступа к этим данным.
Ручной прогон всегда имеет `ci: null`, даже если его пользовательское поле
`metadata` содержит похожие ключи.

`status` относится к сбору покрытия, `pipelineStatus` — к GitLab pipeline,
а `jobStatus` — к отдельной job. Pipeline может закончиться со статусом
`failed` при корректно завершённом сборе. Для сравнения передайте выбранные
`runIds` и `baselineRunId` в `POST /api/runs/compare`. Сохраняются действующие
проверки завершённости сбора и совпадения снимков.

## Просмотр jobs

`GET /api/runs/{runId}/gitlab/jobs?limit=100&offset=0` возвращает `total` и
страницу jobs в порядке убывания `jobId`. У каждой job есть имя, stage, статус,
ссылка `url` и даты создания, запуска, завершения. Пока `jobsSyncedAt: null`,
полный проход страниц ещё не завершён и список может быть частичным.
`jobsFinalized: true` означает, что страницы были считаны после фиксации
завершения pipeline. Старый endpoint `GET /api/runs/{runId}/gitlab` также
возвращает ветку, SHA, окружение и ID проекта при наличии привязки.

История CI и jobs доступны с grant на сервис, администратору или доверенному CLI.
Для обычного просмотра публичного сервиса сохраняется общая история прогонов.
Запрос с CI фильтром или сортировкой без grant возвращает `404`.

## Настройка и обновление данных

После `alembic upgrade head` фоновый процесс читает SHA и jobs для
привязанных к прогонам GitLab pipelines. Он включает попытки повторно запущенных
jobs и хранит полученные страницы в PostgreSQL. История читается из базы
даже при временной недоступности GitLab. Синхронизируются jobs самого
привязанного pipeline. Если он запускает дочерний pipeline, его jobs можно
посмотреть в GitLab, перейдя по ссылке на родительский pipeline.

Окружение сбора покрытия можно указать в серверном `GITLAB_SERVICE_PROJECTS`:

```json
{"<service UUID>": {"projectId": 12345, "environment": "qa/staging"}}
```

Сокращённая привязка `{"<service UUID>": 12345}` продолжает работать.
Окружение сохраняется при создании GitLab прогона. Это метка целевого
окружения покрытия для сервиса. Миграция оставляет окружения существующих
прогонов пустыми. Синхронизация использует только настроенный origin и
проект, привязанный к сервису.

Контракты GitLab: [Pipelines API](https://docs.gitlab.com/api/pipelines/),
[Jobs API](https://docs.gitlab.com/api/jobs/).
