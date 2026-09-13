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
- [x] **Backend.** Доводка PR #1035 по находкам ai-review (критерий 4 задачи #1034: смотри
      `docs/agents/PROTOCOL.md`) — присвоить финальному ADR PR #1035 номер через тот же
      механизм. Готово: ADR перенумерован 0019 → 0021 (`decision_numbering.py next` на
      живом состоянии репозитория), критерии 4/6 починены по находкам ai-review, добавлен
      раздел «Правило» (шесть величин → действие).
- [x] **Backend.** Доводка PR #1082 по находкам второго гейта (ai-review). Блокирующие:
      1) `parse_numbered_files` схлопывал дубль номера ВНУТРИ одного источника (main сразу
      после того, как два PR слились без git-конфликта) — теперь список, не строка;
      2) периодического инварианта не было — добавлена готовая обвязка
      `check_decision_doc_number_collisions`, подключение в `repo_invariants.py` отложено
      до слияния PR #1076 (конкуренция за тот же файл, ADR 0016) — задача #1090. Backlog,
      тоже почищено: `--diff-filter=A` терял переименование унаследованного файла (нужен
      `AR`), честное сообщение «коллизий нет» при сужении по self, арифметика цены
      миграции в design.md (17+15=32, не 12+15=27), нестабильный путь `openspec/changes/
      .../design.md` в докстринге модуля (архивируется, путь протухнет).
