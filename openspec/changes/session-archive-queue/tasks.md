# tasks: session-archive-queue

Исполнитель: worker-агент (PR #1418). Критерий приёмки: гвардии в
`scripts/orchestra/test_scheduler.py` зелёные, мутации по каждой исполнены.

- [x] Метка `session:archive-pending` ставится до попытки и до логина в
      морду; снимается только на успехе или терминальном
      `session-not-found` (`scheduler.py::archive_runner_sessions`).
- [x] Догоняющий проход `retry_pending_session_archives` вызывается на
      каждом пульсе `main()`.
- [x] Гейт архива в `after_merge` — задача ВЕТКИ (`archive_targets`), не
      упоминания тела; PR без agent-ветки морду не трогает (находка
      ai-review PR #1418).
- [x] Метка внесена в `docs/agents/LABELS.md` с газом (автоснятие на
      успехе/`session-not-found`; повтор на каждом проходе).
- [x] Дельта-спека заменяет сценарий архива
      `runner-sessions-in-dsh-morde` (находка ai-review PR #1418, п.2).
- [ ] Пост-мерж проверка: первый проход оркестратора после слияния
      показывает либо распадающуюся очередь, либо строки 🚨 с причиной —
      и то и другое видно в отчёте/по метке без ручного разбора.
