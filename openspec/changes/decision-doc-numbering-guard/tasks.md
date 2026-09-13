# Задачи: #1078

- [x] **Backend.** `scripts/lib/decision_numbering.py` — чистые функции
      (`parse_numbered_files`, `next_free_number`, `find_number_collisions`) + CLI
      (`next`, `check`) с IO через `git`/`gh`. Критерий: функции без сети, тестируемые
      мутацией.
- [x] **Backend.** `scripts/lib/test_decision_numbering.py` — юнит-тесты чистых функций
      (мутация: снять тело `find_number_collisions` → тест красный) + поведенческий тест
      на реальном временном git-репозитории (два "PR"-ветки с коллизией номера).
- [x] **Backend.** `scripts/ci/guards/decision-doc-numbering-guard.sh` — регистрация в
      каталоге гвардий (#749), без правки `repo-ci.yml`.
- [x] **Backend.** ADR, фиксирующий решение (сквозная нумерация + гвардия, не смена схемы;
      не git-ref-lock) — `docs/decisions/0020-decision-doc-numbering-guard.md`, номер
      получен из самого механизма (`decision_numbering.py next docs/decisions`), не руками.
- [ ] **Backend.** Доводка PR #1035 по находкам ai-review (критерий 4 задачи #1034: смотри
      `docs/agents/PROTOCOL.md`) — присвоить финальному ADR PR #1035 номер через тот же
      механизм.
