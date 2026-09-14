# Задачи: #904

- [x] **Backend.** `scripts/lib/invariant_numbering.py` — чистые функции
      (`parse_registry_entries`) + IO через `git` (`read_blob_at_ref`,
      `added_registry_entries`, `collect_sources_from_refs`) + CLI (`next`, `check`).
      Переиспользует `find_number_collisions`/газовые примитивы `decision_numbering.py`
      импортом, не копией. Критерий: чистые функции без сети, тестируемые мутацией.
- [x] **Backend.** `scripts/lib/test_invariant_numbering.py` — юнит-тесты чистых функций
      + поведенческий тест на реальном временном git-репозитории с ДОСЛОВНОЙ фикстурой
      живой коллизии #904 (PR #1061 против PR #1136 на номере 19, текст скопирован из
      реальных веток на 2026-09-14). Мутация доказана дословно (см. отчёт PR): снятие
      `find_number_collisions` → 6 тестов красных; возврат `added_registry_entries` к
      полному реестру ветки → 3 красных; возврат к тройной точке в diff → `no merge base`,
      4 красных. Все три отменены, 18/18 зелёных.
- [x] **Backend.** `scripts/ci/guards/invariant-numbering-guard.sh` — регистрация в
      каталоге гвардий (#749), без правки `repo-ci.yml`.
- [x] **Backend.** `cmd_check` спроектирован без live-дефекта, найденного в
      `decision_numbering.py` (main не виновен в коллизии с ещё не смёрженным сторонним
      PR, только в коллизии внутри самого себя) — см. design.md.
- [x] **Backend.** `check_invariant_number_collisions` — обвязка под `check_result.
      CheckResult`, НЕ подключена в `repo_invariants.py` (файл занят PR #1136/#1189) —
      подключение вынесено отдельной узкой задачей, тот же порядок, что #1090 у
      `decision_numbering.check_decision_doc_number_collisions`.
- [x] **Backend.** design.md — цена миграции на строковый ключ посчитана (места,
      знающие номер), решение — не мигрировать сейчас, обоснование приведено.
- [x] **Backend (отдельная задача, не в объёме #904).** Заведён issue #1200 про живой
      дефект `decision_numbering.py::cmd_check` (main наказан за коллизию со сторонним,
      ещё не смёрженным PR — пост-мерж прогон repo-ci.yml красный 12+ прогонов подряд на
      2026-09-14) — не чинится этим PR.
