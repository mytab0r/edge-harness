# agent-uid-isolation: dsh идёт под выделенным юзером без docker (#140)

Задача: #140. Замер: [docs/research/40-model-shell-key-exposure.md](../../docs/research/40-model-shell-key-exposure.md).
Дельта-спека: `specs/runner-isolation/spec.md` — новая способность «изоляция
адаптера модели на раннере».

## Класс закрываемой ошибки

Заявка «агент ключ не видит» опиралась на одно вырезание env
(`*KEY*/*TOKEN*/*SECRET*`) в model-shell вызовах. Замер 2026-09-05 живым
прогоном из model-shell:

1. Прямое чтение `/proc/<pid-dsh>/environ` — запрещено ядром (yama
   `ptrace_scope=1`: environ читается только у собственных потомков). Вектор
   из названия задачи **не подтвердился**.
2. Тот же класс **подтвердился через docker-сокет** (группа docker — дефолт
   образа раннера): контейнер `--pid=host --cap-add SYS_PTRACE
   --security-opt apparmor=unconfined` прочитал environ dsh, где лежат
   `DEEPSEEK_API_KEY`, `GH_TOKEN`, `HANDS_TOKEN`, `TELEGRAM_BOT_TOKEN`,
   `DSH_EDGE_ACCESS_KEY`.

## Решение

«Отдельный uid» из критерия задачи, одно место правды —
`dsh_agent_isolation_prepare` / `dsh_agent_run` в `scripts/lib/dsh-ci.sh`,
врезано во все три обёртки (worker `task.sh`, hands `dsh_task.sh`,
ai-review `ai_dsh.sh` — класс #93/#94):

- dsh стартует под выделенным непривилегированным юзером без группы docker;
  model-shell наследует uid, сокет docker ему недоступен (EACCES), environ
  транспорта (другой uid) недоступен, /proc-вектор по-прежнему закрыт ядром;
- секреты едут к dsh через sudoers `env_keep` (память sudo), не через argv
  (`/proc/*/cmdline` читаем всеми) и не через файлы (домен агента читает сам
  агент); `GH_TOKEN`/`HANDS_TOKEN`/`TELEGRAM_*`/`DSH_EDGE_ACCESS_KEY` в env dsh
  больше не попадают вовсе;
- воркспейс и каталог спула передаются агенту; gh-конфиг зеркалится (режим
  `gh`) либо сносится с проверкой отсутствия (режим `nogh`, граница #18);
- подготовка доказывает изоляцию до запуска dsh (env-проводка, docker-denied,
  environ-denied на настоящем uid-барьере) и падает громко при отказе —
  запуск без изоляции запрещён;
- гвардии: `scripts/lib/test/agent-isolation.guard.sh` в repo-ci (настоящий
  sudo, свойства как факты окружения) + smoke bash-клиентов (прогон dsh обязан
  идти через `dsh_agent_run`).

## Что вне рамок

- PAT в `~/.config/gh/hosts.yml` остаётся читаемым моделью по построению
  (единственный gh-канал, переживающий env-вырезание; модель обязана уметь
  открывать PR). Зафиксировано в замере как осознанная граница.
- Траты квоты провайдера легальными вызовами модели — штатная работа агента.
- Задачи, которым нужен docker на раннере: эскалация владельцу (blocked).
  Газ — вынос ключа с раннера; решение владельца, не воркера.
