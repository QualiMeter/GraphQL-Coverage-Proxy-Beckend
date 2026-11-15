# Outbound transport и SSRF: этап 2

`app/upstream.py` — единая политика для endpoint CRUD, proxy и временной legacy
introspection. На сохранении и перед каждым обменом проверяются URL и все
DNS-ответы. Production разрешает только HTTPS:443, без userinfo, query (любой,
включая пустой `?`), fragment, zone ID, неоднозначной authority и управляющих символов.
IPv4/IPv6 должны быть global; IPv4-mapped адреса проверяются как IPv4.
Loopback/private/link-local/metadata/multicast/unspecified/reserved запрещены,
включая AWS IPv6 metadata fd00:ec2::254, Azure WireServer 168.63.129.16 и IPv6 transition ranges NAT64/6to4/Teredo.
Любой запрещённый DNS-ответ блокирует имя целиком. DNS timeout — 3 с, до 32 ответов.

**DNS rebinding:** после проверки создаётся immutable Destination с числовыми IP.
Публичный `httpcore.AsyncNetworkBackend` подключает сокет только к первому
проверенному IP; дополнительное разрешение исходного имени не производится.
Проверяется фактический peer IP/port. Исходные origin/Host остаются в HTTP,
TLS SNI и certificate hostname validation используют исходное имя.
SSL context требует доверенную цепочку и hostname; caller extensions не могут
заменить SNI. Публичный AsyncBaseTransport адаптирует httpcore к HTTPX без
изменения его приватных атрибутов. Environment proxies, redirects и retries выключены.
Каждый обмен имеет отдельный pool/client/cookie jar, повторов на другой IP нет.
Если первый DNS-адрес недоступен, возвращается безопасная ошибка; следующий обмен
повторно проверяет DNS. Автоматический retry mutation не выполняется.

HTTP/local для разработки требуют **оба** COVERAGE_DEV_MODE=true и точного origin
в COVERAGE_DEV_UPSTREAM_ORIGINS (scheme, hostname/IP, port). Wildcards/suffix
сопоставления нет. Разрешение одного порта не разрешает соседние. Даже в dev
запрещены metadata/link-local, multicast, reserved (кроме явно разрешённого loopback),
низкие порты кроме 80/443 и известные DB/mail/admin порты. Production Compose
принудительно оставляет dev false. Это локальная конфигурация, не production allowlist.

Лимиты: service DTO 16 КиБ, proxy request 1 МиБ / 10 с на чтение,
raw response 8 МиБ; decoded копия для анализа 1 МиБ. Обмен upstream ограничен
30 с общим временем и 30 с на сетевую операцию, 16 одновременных обменов на
worker (переполнение 503 без отправки). Ответ буферизуется, потоковая доставка
и WebSocket не поддержаны. Legacy introspection принимает только identity encoding,
до 8 МиБ и 15 с. Тела, query/variables/credentials/ответы не пишутся в БД или логи.
Не включать стороннее middleware/debug-логирование тел и заголовков.

Service row FOR UPDATE сериализует создание run и изменение конфигурации.
После получения блокировки смена endpoint проверяет running runs через snapshots
этого сервиса; существующие индексы service_id/snapshot_id и partial running index
обслуживают проверку. Lock не удерживается во время HTTP. Finish/запись наблюдения
сохраняют существующую блокировку run. Если finish опережает запись после HTTP,
502 явно предупреждает о возможном выполнении операции, без повторного HTTP.

Граница доверия: ACL ограничивает выбор сервиса; DNS pinning ограничивает адрес
сокета. TLS защищает оригинальный HTTPS hostname. Это не защита от администратора
с доступом к исходникам/сетевой инфраструктуре или от публичного upstream, который
сам является прокси. Сетевой egress firewall можно добавить как дополнительный слой,
но корректность текущей защиты не зависит от проверки одной строки URL.
