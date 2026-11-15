# Этап 2: миграция и будущая выкладка

Это инструкция оператору, **не разрешение на deploy**. Production, secrets,
repository variables, Nginx и работающие контейнеры в этом PR не менялись.
Следовать [порядку безопасного слияния](PR23-RELEASE.md): оператор подтверждает
выключенный ENABLE_PRODUCTION_DEPLOY и отсутствие ожидающих deploy jobs до merge;
после merge backend CI должен пройти, deploy — skipped. Автодеплой не включать
для проверки этапа 2. Автор PR не выполняет merge или workflow_dispatch на main.

Миграция `20261116_service_endpoints`, после `20261115_auth`:

- nullable endpoint_url VARCHAR(2048) и description VARCHAR(2000);
- updated_at TIMESTAMPTZ, существующие строки получают свой created_at,
  затем NOT NULL + default now();
- нет произвольного backfill URL, удаления/перезаписи snapshots, runs, observations;
- прежние индексы и Coverage Engine сохраняются. Временный DDL lock на services
  требует обычного согласованного окна миграции и backup; на большой таблице
  учитывать UPDATE и блокировку. Изменений production БД сейчас нет.

До будущего согласованного выпуска:

1. На staging сделать backup и прогнать upgrade head на копии исторических данных.
2. Инвентаризировать старые сервисы и реальные upstream. Не копировать один
   TARGET_GRAPHQL_URL во все сервисы. Дождаться завершения активных runs:
   смена NULL endpoint на реальный URL тоже блокируется при running run.
3. Запланировать отдельный переход clients на run ID. Новый default выключает
   старый неявный passthrough. При доказанной необходимости оператор временно
   задаёт COVERAGE_LEGACY_PROXY=true + проверенный HTTPS TARGET_GRAPHQL_URL.
   Это fallback только для старых NULL endpoint и доверенного CLI без run ID;
   configured-сервисы всегда используют свой endpoint, browser требует run ID.
4. В будущем принятом образе выполнить миграцию, настроить endpointUrl через
   admin/operator API с реальными URL и grants, отключить legacy после перехода.
   Не редактировать production данные напрямую ради обхода блокировки.
5. Проверить egress DNS/TLS, доступность первого безопасного DNS IP, HTTPS:443,
   лимиты 1/8 МиБ и 30 с, cookie/CSRF за существующим доверенным reverse proxy,
   два независимых run и отсутствие приватных endpoint в anonymous API.
   HTTP/private upstream в production преднамеренно запрещены; dev false
   зафиксирован production Compose. Внутренние upstream потребуют отдельного
   согласованного проектирования, а не отключения SSRF.
6. Начиная с этапа 3 configured-сервисы импортируют introspection собственного
   endpoint. При INTROSPECTION_DISABLED (409) или необходимости upstream credentials
   используется ручной SDL; см. [контракт](../docs/frontend-introspection.md).

Rollback приложения не требует downgrade: nullable новые поля старая версия
игнорирует. Однако старая версия снова маршрутизирует глобально; **остановить
multi-service трафик перед откатом**, иначе запрос может уйти чужому upstream.
Не запускать автоматический downgrade: он удалит новые настройки endpoint/description.
Не восстанавливать всю БД без отдельного решения о потере новых observations.
