# Дизайн: dsh-web-on-demand

Смежное: [proposal](proposal.md), [задачи](tasks.md),
[архитектура DSH](../../../docs/research/10-dsh-architecture.md),
[dsh-edge](../../../docs/research/11-dsh-edge.md),
[GitHub Actions](../../../docs/research/21-github-actions.md),
[отвергнутые варианты](../../../docs/research/30-rejected-alternatives.md).

Все утверждения о DSH ниже — из чтения tarball'ов `@deepseek-ai/dsh-*` 0.1.1-rc.2
(скачаны `npm pack` 2026-08-29); файлы-улики процитированы по месту. Утверждения о
tailscale — из README экшена, `action.yml`, KB ephemeral-узлов и KB/CLI-справочника
`tailscale serve` и исходника `ipn/ipnlocal/serve.go` (2026-08-29).

## Форма

```
владелец (браузер, устройство своего tailnet)
  │  https://dsh-web.<tailnet>.ts.net
  ▼
tailscale serve  (эфемерный узел job'а; TLS-сертификат автоматом, Host сохраняется)
  │  http://127.0.0.1:3080
  ▼
dsh --profile web   (SPA + WS; Host/Origin-fence, --trusted-host <FQDN узла>)
  ├─ ~/.dsh  ◀── restore / save (if: always) ──▶ GH Actions cache (10 GB/репо, eviction 7 дней)
  └─ env DEEPSEEK_BASE_URL / DEEPSEEK_API_KEY → GLM coding-эндпоинт + glm-5

Морда (Workers Assets), DO-журнал, dsh-edge — в цепочке НЕ участвуют:
ни одного запроса к CF, ни одного события в journal-seq.
```

Триггер — отдельный `workflow_dispatch` «открыть рабочий стол». Job живёт, пока идёт
рабочая сессия (ввод владельца: длительность в минутах), и умирает по её конце —
внутренним таймаутом, ручной отменой из вкладки Actions или 6-часовым потолком
раннера ([research/21](../../../docs/research/21-github-actions.md)).

## Решения

**Подъём — `dsh web`, bind остаётся 127.0.0.1:3080, наружу — только `tailscale serve`.
Интерфейс не перебиваем.** В CLI дверь закрыта явно (`dsh-web-app/lib/startup.js:40`,
дословно): `--host 0.0.0.0` → «error: --host 0.0.0.0 is intentionally not supported yet
for safety: it would expose remote code execution to the network; use 127.0.0.1
instead». Схема конфига webserver'а допускает только два литерала — `127.0.0.1` и
`0.0.0.0` (`dsh-host-webserver/lib/index.js:99`), привязаться к tailnet-интерфейсу
нельзя даже патчем без потери апгрейдимости. Дефолты берутся из бандл-патча
(`dsh-web-app/cordis.patch.yml`, ряд `webserver`): `host: 127.0.0.1`,
`port: 3080`. Флаги запуска: `--host`, `--no-open`, `--port`, `--trusted-host
<authority...>` («extra authority the /api browser-trust fence accepts (host or
host:port; repeatable)»). В headless-режиме раннера открываем с `--no-open` — попытка
поднять браузер на раннере шумит и ничего не даёт.

**Проброс наружу — `tailscale serve --bg 3080`.** Ровно поддерживаемая форма прокси:
цель — «a port number (for example, 3000)…», ограничение — «only `http://127.0.0.1`
is supported for proxies» (KB serve). TLS — «automatically provisioned TLS
certificate… the device's Tailscale daemon terminates the HTTPS connection»; адрес —
`https://dsh-web.<tailnet>.ts.net`. Никаких дырок в фаерволе раннера (входящих к
раннеру нет и не нужно — serve держит исходящее соединение с координатором tailnet).

**Доверие — Host/Origin-fence плюс `--trusted-host`; своей аутентификации в web-профиле
НЕТ, границей безопасности является сам tailnet.** Поставляемый fence
(`dsh-client-connection/lib/index.js`, «Browser-trust fence for every /api request»)
пускает запрос, только если Host — loopback или входит в `trustedHosts`; `sec-fetch-site:
cross-site` отклоняется; присутствующий Origin обязан совпасть с Host. WebSocket-апгрейды
(`/api/remote.mux` и host-events) проходят тот же fence до согласования протокола.
Документация самого шва отрезает лишние ожидания, дословно: «Network reachability and
authentication stay out of scope: binding policy belongs to the webserver config, and
this fence is not an auth layer». В коде rc.2 ни логина, ни токена, ни куки нет —
URL печатается голый: `dsh web: http://127.0.0.1:<порт>`. Следствие принимается явно:
**доступ к UI = доступ к любому устройству tailnet владельца**; персональный tailnet —
уже граница «только владелец», и это осознанный выбор, а не недосмотр.

**`--trusted-host` обязателен и выводится рантаймом.** Прокси serve **сохраняет**
Host входящего запроса — `ipn/ipnlocal/serve.go`, в `Rewrite`: `r.Out.Host = r.In.Host`
(замена на адрес цели только для unix-сокетов), оригинал дублируется в
`X-Forwarded-Host/Proto/For`. Браузер шлёт `Host: dsh-web.<tailnet>.ts.net`, fence без
флага отвечает 403 на каждый `/api` — SPA откроется и будет тихо мёртвой. Поэтому job
до старта dsh выводит DNS-имя узла (`tailscale status --json` → `.Self.DNSName`, хвост
`.` срезается — сверить в PoC) и запускает `dsh web --trusted-host <FQDN>`. Форма
записи «port-less host» совпадает с документацией шва: такой вход «matches the hostname
on any port». Здоровье границы доказывается запросом, а не надеждой: job проверяет
`https://<FQDN>` до передачи URL владельцу, ответ fence `403 forbidden` — громкое
падение с подсказкой, а не «страница открылась, а работать не работает».

**Подключение к tailnet — официальный `tailscale/github-action`, эфемерный узел.**
Механика из `action.yml`/README экшена: входы `oauth-client-id` + `oauth-secret`
(«an OAuth client for the tailnet… must have the writable auth_keys scope»), `tags`
(«At least one tag is required» — узел OAuth-клиента обязан быть тегирован, тег
создаётся в политике tailnet заранее), `hostname` (фиксированный DNS-лейбл; берём
`dsh-web`, чтобы имя и закладка не менялись между сессиями), `version` (пин), `ping`
(«will wait up to 3 minutes for a connection» — проверка достижимости до старта dsh).
Эфемерность — документированное свойство: «Nodes created by this Action are marked as
Ephemeral» и «log out immediately after finishing their CI run, at which point they
are automatically removed» (post-шаг экшена делает logout). KB ephemeral-узлов: узлы
«auto-removed… normally from 30 to 60 minutes after the last activity», logout удаляет
немедленно, «the next time an ephemeral node is created, it will have a new IP
address» — новый IP на сессию, постоянного адреса нет. **Вход от владельца (один раз):
создать OAuth-клиент с auth_keys-скоупом и тег `tag:ci` в политике tailnet; секреты
`TS_OAUTH_CLIENT_ID`/`TS_OAUTH_SECRET` — в Actions secrets.** Альтернатива —
pre-signed ephemeral reusable auth key (она же обязательна при Tailnet Lock: «Authenticate
using an ephemeral reusable pre-signed auth key rather than an OAuth client»); README
помечает authkey устаревающим входом («An OAuth API client is recommended instead of an
authkey»), поэтому путь по умолчанию — OAuth-клиент.

**Персистентность `~/.dsh` — GH Actions cache; путь по умолчанию. R2 — запасной.**
Факты по кэшу: «By default, the limit is 10 GB per repository»; eviction — «deleting
the caches in order of last access date, from oldest to most recent», плюс «GitHub will
remove any cache entries that have not been accessed in over 7 days»; содержимое
кэша иммутабельно («You cannot change the contents of an existing cache»); чтение —
из своей ветки или default-ветки, разным workflow одного репо кэш доступен; и главное
ловушка: «the action automatically creates a new cache if the job completes
successfully» — job, убитый таймаутом, **не сохранит** кэш. Поэтому связка
`actions/cache/restore` + отдельный шаг `actions/cache/save` с `if: always()` —
сохранение обязано случаться и после отмены, и после внутреннего таймаута. Кэшируется
`~/.dsh` целиком: плагины профилей, `settings.yaml`, `.credentials.yaml`-ссылки,
сессии (`sessions/--<cwd>--/<id>/session.jsonl.zstd`).

> **Засада с pnpm-store, закрыть до продакшна.** `dsh plugin add` — форвардер в pnpm
> внутри каталога профиля; pnpm кладёт содержимое в глобальный content-addressable
> store **вне** `~/.dsh`, а в профиле остаются симлинки. Кэш только `~/.dsh` молча
> восстановит битые симлинки — класс silent-wrong. Лечение одной строкой: пиновать
> store внутрь кэшируемого дерева (`pnpm config set store-dir ~/.dsh/.pnpm-store`)
> до первой установки плагина. Проверка — в [tasks](tasks.md), PoC.

Путь по умолчанию — именно cache, а не R2: кэш не требует ни нового бакета, ни новых
секретов в CI, ни кода синхронизации, а его единственный реальный риск (eviction после
7 дней простоя) по построению превращается в громкое событие: при пустом `~/.dsh` job
пишет в summary «плагины переустанавливаются с нуля» и переустанавливает. Возврат к
R2 — по факту первой боли (eviction начал вредить, 10 GB кончаются, или плагинов
станет настолько много, что переустановка станет заметной частью старта сессии), не
по прогнозу.

**Провайдер — env `DEEPSEEK_*` поверх web-профиля, тот же, что у headless.**
Адаптер `dsh-llm-deepseek` читает из env только `DEEPSEEK_BASE_URL` и
`DEEPSEEK_API_KEY` (плюс `DEEPSEEK_REASONING`); `DEEPSEEK_MODEL` dsh не читает.
Модель пинится патчем профиля `~/.dsh/profiles/web/cordis.patch.yml`
(`agent-default-model: {provider: deepseek-official, model: <модель>}` +
`llm-deepseek: {maxTokens: 131072}` — потолок GLM; дефолтные 256 000 дают
`INVALID_REQUEST`). Рабочая связка та же, что у рук: GLM coding-эндпоинт +
`glm-5`, значения — из `vars` репозитория, как в `hands.yml`. Job пишет патч
идемпотентно перед каждым стартом — файл в кэше может быть свежее или старее кода,
правда живёт в workflow, а не в кэше. Следствие для UI: provider `deepseek-official`
с env-ключом виден в пикере моделей, и страница Settings/Models web-морды управляет
провайдерами штатно; поведение «правка провайдера через UI поверх env» — не
подтверждено (см. ниже).

**Жизненный цикл — отдельный workflow_dispatch «открыть рабочий стол», не внутрь
обычной задачи.** Обычная hands-задача живёт минуты и обязана умирать по завершении;
рабочее место живёт, пока владелец работает, — это другой цикл жизни, и смешивать их
значит привязать смерть рабочего места к концу случайной задачи. Входы: длительность
в минутах (дефолт 240, потолок ниже жёсткого 6-часового лимита job'а). Стоп — три
пути: внутренний `timeout` на dsh-процессе с запасом до конца job'а (graceful,
с флашем и сохранением кэша), ручная отмена run'а из вкладки Actions (владелец имеет
write), 6-часовой потолок раннера как аварийная граница. Доплата за схему — ноль:
standard-раннеры публичного репозитория бесплатны, планировщика и keepalive нет —
никто не продлевает жизнь job'а искусственно.

**Незавершённый ход.** Живой процесс с концом сессии умирает, но durable-лог
самолечится: «после краха незакрытый `turn/start` дописывается синтетическим
`turn/end { reason: { kind: 'interrupted' } }`» (research/10, «Персистентность»);
сессия остаётся в `~/.dsh`, resumes из кэша в следующей сессии. Честная цена:
состояние драйвера не сериализуется, инбокс теряется, а персистенция DSH пишет
асинхронно («the hot path never blocks on I/O — persistence plugins buffer
asynchronously») — хвост событий между последним флашем и SIGKILL может не доехать
до диска. Дизайн принимает это как границу: внутренний таймаут с TERM-фазой
сводит окно к секундам; размер реальной потери замеряет PoC-прогон (убийство
середины хода → resume → сверка лога).

**Правовая рамка — по смыслу Terms, осознанно и с границами.** Запрещено «any other
activity unrelated to the production, testing, deployment, or publication of the
software project associated with the repository» и «any activity that places a burden
on our servers, where that burden is disproportionate…» (цитаты и санкции —
[research/21](../../../docs/research/21-github-actions.md)). Отличия от отвергнутого #3,
каждое по существу той причины:

1. **Нет туннеля через CF и его цены.** В #3 job держал исходящий WS к DO: 85 %
   дневного GB-s Free и неработающая гибернация. Здесь CF в цепочке отсутствует
   целиком — экономическая причина отказа не возникает.
2. **Job не простаивает в ожидании внешних команд.** В #3 сокет был каналом
   произвольных команд, job — приёмником. Здесь job исполняет рабочую сессию над
   софтом этого репозитория (агент-процесс активен всю сессию), а UI — окно
   владельца в ту же работу: тот же класс активности, что у hands-job слайса 1,
   только интерактивный и длиннее.
3. **Ограниченность по построению.** Ручной запуск под конкретную сессию, вход
   «длительность» ниже 6-часового потолка, никаких расписаний, keepalive и эстафет
   между job'ами — схему нельзя превратить в «постоянно доступный бесплатный
   компьютер», соблазн которого разобран в вердикте research/21.

Остаток назван честно: явного пункта про «интерактивную сессию через tailnet» в Terms
нет — это чтение по смыслу, как и весь раздел; держим сессии привязанными к работе над
репозиторием и не масштабируем частоту без перепроверки рамки.

**Морда и DO не участвуют.** Ни событий в журнал, ни запросов к DO: лёгкий чат
dsh-edge остаётся единственным always-on каналом между сессиями, journal-seq не
получает второго писателя, бюджет DO не тратится. Сессии web-морды живут только в
`~/.dsh` и в морде не проекцируются — наблюдаемость хода задачи остаётся за слайсом
стриминга, это разные поверхности.

## Отвергнутые варианты

**Поднять UI с `--host 0.0.0.0` на tailnet-интерфейс.** Запрещено CLI намеренно
(цитата выше), схема конфига допускает только два литерала. Обход патчем — вилка от
апстрима ради того, что штатно решает serve с бесплатным TLS. Плюс без serve адрес
узла меняется каждую сессию.

**Персистентность в R2 сразу.** Требует бакет, secrets в CI и собственный код
синхронизации/ретраев — вторая инфраструктура и второй режим отказа ради риска,
который кэш закрывает громким переустроем. Возврат — по замеру из PoC (см. решение
о персистентности), не по прогнозу.

**Держать web-профиль внутри обычной hands-задачи (флаг к задаче).** Смерть рабочего
места привязывается к случайной задаче; таймаут задачи (30 мин) не совместим с
рабочей сессией; heartbeat-конвейер журнала не нужен UI и засоряет его. Разные
циклы жизни — разные воркфлоу.

**Экспонировать UI через Cloudflare Tunnel / Funnel вместо tailnet.** Разворачивает
доступ наружу (Funnel — публичный интернет) и снова втягивает CF в цепочку, от
которой эта схема уходит; аутентификации в web-профиле нет (fence — «not an auth
layer»), границей может быть только закрытая сеть. Tailnet — это и есть закрытая
сеть владельца.

**Сборка морды-агрегатора поверх UI (мост событий web-сессии в журнал).** Возврат
отвергнутого #3 через боковую дверь: появился бы долгоживущий канал job → CF и
второй писатель в journal-seq. Ничего из этого дизайн не строит.

## Чего дизайн намеренно не решает

- Проекцию web-сессий в морде: журнал показывает задачи hands-конвейера; web-сессия
  живёт в своём UI и в DO не зеркалится.
- Мульти-сессии и параллельные рабочие места: одна сессия за раз (concurrency-группа
  воркфлоу), второй запрос ждёт.
- Автоматическую переустановку конкретного набора плагинов из кода репозитория:
  плагины — состояние владельца в `~/.dsh`, не артефакт репо; дизайн даёт громкую
  переустановку и пин pnpm-store, а не «манифест плагинов».
- Follow-up задач из морды и всё, что меняет контракт dsh-headless.
- Секреты в UI-сессиях: ключ провайдера виден процессу dsh по построению; редакция
  журнала слайса 1 сюда не применяется, потому что в DO ничего не уходит.

## Не подтверждено

1. **Сертификаты и `serve` на эфемерном узле.** KB serve не описывает эфемерные узлы;
   выпуск TLS-сертификата для `dsh-web.<tailnet>.ts.net` на свежем узле каждой сессии
   не проверен (включённость MagicDNS/HTTPS на tailnet — разовая настройка владельца).
   Фолбэк: `tailscale serve --http=80` (документирован) — доступ по http внутри
   tailnet. Первый шаг PoC.
2. **Месячная бесплатная квота эфемерных минут.** KB: «Ephemeral node usage is
   included at no cost up to a monthly limit (measured in minutes)»; значение квоты и
   её зависимость от плана владельца не названы.
3. **Поведение dsh web при SIGTERM/SIGKILL и окно потери хвоста сессии** (см.
   «Незавершённый ход»): флашит ли dsh web по TERM, какова частота автонаписания
   персистенции в web-профиле, какой grace-период даёт GH при отмене run'а.
4. **Точный формат `.Self.DNSName`** (хвостовая точка, IDN) и его каноническое
   совпадение с авторитетом fence — сверяется в PoC-прогоне до продакшн-скрипта.
5. **Управление провайдерами через UI поверх env `DEEPSEEK_*`.** Страница
   Settings/Models штатно правит провайдеров (`llm-pi-ai` секция settings), но что
   побеждает при конфликте env-адаптера и settings-правок, куда UI пишет ключи
   (`settings.yaml`/`.credentials.yaml` кэшируемого `~/.dsh`) — не проверено.
6. **Установка плагина через UI web-профиля** попадает в `~/.dsh/profiles/web`
   (механика CLI `dsh plugin` — форвардер в pnpm; путь из UI не трассирован).
   Подтверждается первым же PoC-прогоном установкой плагина через UI.
7. **`npm install -g` tarball'а dsh тянет web-дистрибутив** (`dsh-web-frontend/dist`,
   резолвится `require.resolve` в dsh-web-app). Для headless-связки транзитивный
   резолв доказан живой установкой (475 пакетов); web-часть — тем же первым прогоном.
8. **«browser-session authentication» в доках апстрима.** `docs/subsystems/web-server.md`
   приписывает shipped `dsh web` «Host/Origin checks plus browser-session
   authentication», в опубликованном коде rc.2 второй части нет. Противоречие живое
   upstream: при смене пина версии перепроверять авторизацию до обновления
   дизайн-фактов.
