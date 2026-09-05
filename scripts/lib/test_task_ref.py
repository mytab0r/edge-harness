#!/usr/bin/env python3
"""Тесты извлечения номера задачи из текста (scripts/lib/task_ref.py, #187, #195, #251).

Класс #187: `f"#{n}" in text` / `line.split("#")` матчат подстрокой — `#18`
совпадает с `#180`, `#181`, `#5180`. Живой прогон orchestra 33570081734:
контракт спутал PR #185 (задача #182) с задачей #18.

Класс #195 (второй экземпляр #187): даже с границами числа `references_task`
по всему телу чужого PR путает «упомянута в прозе» с «объявлена как задача
PR» — contract_check.py сравнивал декларацию своего PR (узко, строка,
начинающаяся с `#N`) с любым упоминанием в теле чужого (широко). Симметрия
восстановлена через task_ref.declared_tasks/declares_task.

Класс #251 (третий экземпляр #187): «декларация — любая строка, начинающаяся
с `#N`» ловила строку прозы, перенесённую по ширине абзаца. Живой инцидент —
тело PR #247 (см. `test_real_pr_247_prose_line_wrap_not_declared` ниже):
строка «  #153, #158, #179`. Из них 7 (...) уходят» — это перенос
перечисления, а не декларация, но старый код читал её как объявление девяти
чужих задач. Правило сузилось до «декларация — только первая непустая строка
тела».

Кейсы кормятся прод-формой тела PR, которая реально встречается в
репозитории (см. test_real_pr_body_form, test_real_pr_209_*,
test_real_pr_247_prose_line_wrap_not_declared).

Запуск: python -m pytest scripts/lib/test_task_ref.py -q
"""

import importlib.util
from pathlib import Path

SCRIPT = Path(__file__).with_name("task_ref.py")
spec = importlib.util.spec_from_file_location("task_ref", SCRIPT)
task_ref = importlib.util.module_from_spec(spec)
spec.loader.exec_module(task_ref)  # type: ignore[union-attr]


def test_no_false_positive_on_longer_number_suffix():
    # #18 не должен матчить #180, #181, #184, #185 (сам баг 33570081734).
    assert task_ref.references_task("Открыт PR #180", 18) is False
    assert task_ref.references_task("Открыт PR #181", 18) is False
    assert task_ref.references_task("Открыт PR #182", 18) is False
    assert task_ref.references_task("Открыт PR #184", 18) is False
    assert task_ref.references_task("Открыт PR #185", 18) is False


def test_no_false_positive_on_longer_number_prefix():
    # #18 не должен матчить #5180 (граница слева — не только справа).
    assert task_ref.references_task("см. задачу #5180", 18) is False
    assert task_ref.references_task("#518", 18) is False


def test_true_positive_various_positions():
    assert task_ref.references_task("#18", 18) is True
    assert task_ref.references_task("Закрывает #18 в этом PR", 18) is True
    assert task_ref.references_task("см. #18, #182 и #185", 18) is True
    assert task_ref.references_task("(#18)", 18) is True
    assert task_ref.references_task("#18,#182", 18) is True


def test_extract_task_refs_multiple_with_boundaries():
    text = "Сначала #18, потом #180 и снова #18 рядом с #5180."
    assert task_ref.extract_task_refs(text) == [18, 180, 18, 5180]


def test_extract_task_refs_empty_text():
    assert task_ref.extract_task_refs("") == []
    assert task_ref.extract_task_refs(None) == []


def test_real_pr_body_form():
    # Прод-форма тела PR (см. контракт PR из AGENTS/скриптов): первая строка
    # ровно "#<N>", дальше пояснительный текст с другими номерами.
    body = (
        "#18\n\n"
        "Второй гейт ревью (AI). Связано с #180, #181, #185 — но задача одна: #18.\n"
    )
    assert task_ref.references_task(body, 18) is True
    assert task_ref.extract_task_refs(body) == [18, 180, 181, 185, 18]


def test_real_pr_body_no_relation_regression():
    # Регресс из 33570081734: PR #185 про задачу #182, контракт для #18 не
    # должен видеть его как конкурента.
    body = "#182\n\nАрхив сессий раннера падает 403 (см. #174).\n"
    assert task_ref.references_task(body, 18) is False
    assert task_ref.references_task(body, 182) is True


def test_declared_tasks_only_from_leading_hash_line():
    # #195: декларация — номер на строке, начинающейся с `#N`, не любое
    # упоминание в прозе (борда GitHub Projects упоминает вехи по номеру).
    body = (
        "#182\n\n"
        "## Что сделано\n"
        "Вехи привязаны к #18 и #20. Строки Зависимости добавлены в тела "
        "#147 и #134.\n"
    )
    assert task_ref.declared_tasks(body) == [182]
    assert task_ref.declares_task(body, 182) is True
    assert task_ref.declares_task(body, 18) is False
    assert task_ref.declares_task(body, 134) is False


def test_declared_tasks_empty_text():
    assert task_ref.declared_tasks("") == []
    assert task_ref.declared_tasks(None) == []


# #195: асимметрия contract_check.py — своя декларация PR уже была узкой
# (строка, начинающаяся с #N), а конфликт с чужими PR гонялся по всему телу
# (task_ref.references_task) вместо декларации (task_ref.declares_task).
# Кейсы ниже — реальные тела PR из живого прогона (не пересказ).

_PR_209_BODY = (
    "#207\n\n"
    "## Что сделано\n"
    "- Правило «Тормоз без газа не принимается» в AGENTS.md (раздел «Правила, "
    "оплаченные чужими ошибками»), с замером цены на пяти реальных тормозах "
    "(#205, #204, #196).\n"
    "- Реестр docs/agents/LABELS.md по всем 13 меткам.\n"
)

_PR_206_BODY = (
    "#205\n\n"
    "## Класс проблемы\n\n"
    "Предохранитель конвейера был двухсостоятельным без сброса: "
    "`conveyor_gate` останавливает диспатч `worker.yml`.\n"
)


def test_real_pr_209_does_not_declare_205_mentioned_in_prose():
    # #209 объявляет #207 первой строкой, #205 упомянут в прозе (замер цены
    # на пяти тормозах) — это не декларация задачи #205.
    assert task_ref.declared_tasks(_PR_209_BODY) == [207]
    assert task_ref.declares_task(_PR_209_BODY, 205) is False
    # references_task (широкая, по всему тексту) видит упоминание — так и
    # должно быть, это не баг references_task, а неверное место её вызова.
    assert task_ref.references_task(_PR_209_BODY, 205) is True


def test_real_pr_206_declares_205():
    assert task_ref.declared_tasks(_PR_206_BODY) == [205]
    assert task_ref.declares_task(_PR_206_BODY, 205) is True


def test_pr_209_and_pr_206_do_not_conflict_on_declared_task():
    # Живой ложный конфликт из #195: contract_check для #206 (декларация
    # #205) находил #209 как «уже открытый PR на задачу #205», хотя #209
    # объявляет #207. По декларации конфликта нет.
    declared_206 = task_ref.declared_tasks(_PR_206_BODY)[0]
    assert task_ref.declares_task(_PR_209_BODY, declared_206) is False


def test_two_prs_declaring_same_task_still_conflict():
    # Обратная проверка: если оба PR ОБЪЯВЛЯЮТ одну и ту же задачу первой
    # строкой — это настоящая гонка веток, конфликт обязан остаться.
    pr_a = "#42\n\nПервая реализация.\n"
    pr_b = "#42\n\nВторая попытка, другая ветка.\n"
    declared = task_ref.declared_tasks(pr_a)[0]
    assert task_ref.declares_task(pr_b, declared) is True


# #251: живой инцидент — тело реально слитого PR #247 репозитория (сохранено
# дословно, `gh api repos/mytab0r/edge-harness/pulls/247 --jq .body`). Строка
# 64 тела — перенос перечисления из абзаца «Замер до/после…», а не
# декларация, но начинается с `#` после снятия ведущих пробелов списка.
_PR_247_BODY = (
    "#245\n"
    "\n"
    "## Что сделано\n"
    "\n"
    "Два дефекта одной функции `free_task()` (`scripts/worker/task.sh`), оба чинятся\n"
    "через новое место правды `scripts/lib/free_task.py`:\n"
    "\n"
    "1. **Скан по всему тексту вместо объявленной задачи** (`scripts/worker/task.sh:118-134`\n"
    "   и вторая копия того же бага в ветке явного входа `--task`, `task.sh:150-155`).\n"
    "   `jq 'scan(\"#[0-9]+\")'` шёл по ВСЕМУ телу открытых PR — любое упоминание номера\n"
    "   в прозе описания делало задачу «занятой».\n"
    "\n"
    "- Замер до/после на живом пуле (2026-09-03): открытых задач без исполнителя —\n"
    "  68. Старый алгоритм (широкий скан прозы PR) признавал доступными 52 из\n"
    "  них; новый — все 68. Разница (ранее ложно заблокированы, теперь доступны):\n"
    "  `#43, #72, #77, #80, #89, #90, #105, #111, #114, #119, #120, #124, #149,\n"
    "  #153, #158, #179`. Из них 7 (`#77, #80, #89, #105, #111, #114, #179`) уходят\n"
    "  в режим доводки существующего PR, а не «новая ветка».\n"
)


def test_real_pr_247_prose_line_wrap_not_declared():
    # Единственная объявленная задача — #245 (первая строка тела). Перенос
    # перечисления `#153, #158, #179...` НЕ должен читаться как декларация
    # девяти чужих задач (#77, #80, #89, #105, #111, #114, #153, #158, #179).
    assert task_ref.declared_tasks(_PR_247_BODY) == [245]
    for foreign in (77, 80, 89, 105, 111, 114, 153, 158, 179):
        assert task_ref.declares_task(_PR_247_BODY, foreign) is False
    assert task_ref.declares_task(_PR_247_BODY, 245) is True


def test_declaration_only_from_true_first_line_not_any_leading_hash_line():
    # Если самая первая непустая строка тела НЕ является декларацией — задач
    # нет вовсе, даже если дальше по тексту есть строка, начинающаяся с `#`
    # (это и есть баг #251: искать декларацию до первой подходящей строки).
    body = (
        "Коротко опиши, что сделано.\n"
        "\n"
        "#118 — смежная задача, не эта.\n"
    )
    assert task_ref.declared_tasks(body) == []
    assert task_ref.declares_task(body, 118) is False


def test_markdown_heading_with_hash_number_is_not_a_declaration():
    # #312: `## Задача #245: …` — markdown-заголовок, начинающийся с `#`, но
    # не декларация (после `#` не цифра, а второй `#`). Докстринг обещал
    # «первая строка ЦЕЛИКОМ декларация», а старый код принимал любую первую
    # строку с ведущим `#`, включая заголовок.
    body = "## Задача #245: контекст\n\nТекст.\n"
    assert task_ref.declared_tasks(body) == []
    assert task_ref.declares_task(body, 245) is False


# #312: живая регрессия сужения из #251. `.github/PULL_REQUEST_TEMPLATE.md`
# начинается с HTML-комментария (`<!-- Правило: … -->`), который GitHub при
# рендере тела PR не вырезает, но и не показывает — значит для человека он
# невидим, а `declared_tasks` (первая непустая строка тела) без вырезания
# комментария видел бы первой строкой `<!--` и терял декларацию `#N` для
# ЛЮБОГО PR, открытого по штатному шаблону через веб-форму. Тест кормится
# самим файлом шаблона (прод-форма), а не пересказом его текста — иначе
# шаблон и тест разойдутся незамеченными.
_TEMPLATE_PATH = Path(__file__).resolve().parents[2] / ".github" / "PULL_REQUEST_TEMPLATE.md"


def test_real_pr_template_html_comment_before_declaration_is_skipped():
    template_body = _TEMPLATE_PATH.read_text(encoding="utf-8")
    assert "<!--" in template_body, "шаблон должен начинаться с HTML-комментария (иначе тест не о том)"
    # Автор PR правит только строку-плейсхолдер `#N`, комментарий-инструкцию
    # не трогает (веб-форма подставляет шаблон целиком, комментарий невидим).
    body = template_body.replace("#N\n", "#245\n", 1)
    assert task_ref.declared_tasks(body) == [245]
    assert task_ref.declares_task(body, 245) is True


# ── Резолвер «PR → задача» (#259) — прод-форма реальных PR этого репозитория ──
#
# Живой замер, который и породил задачу: ai_review.py:353 брал ЛЮБОЕ #N из
# прозы тела, сортировал по возрастанию и судил PR по первому попавшемуся
# открытому issue с меткой task — #253 судили по #120 (упомянут в прозе,
# «issue #120 + Telegram»), хотя ветка и первая строка тела объявляют #227.

_PR_253_BODY = (
    "#227\n\n"
    "## Что сделано\n\n"
    "Стадия приёмки: слитый PR больше не оставляет задачу висеть с "
    "комментарием-напоминанием «исполнителю», которого к тому моменту уже "
    "нет (job воркера завершился).\n\n"
    "3. Три исхода, ни один не тихий: … проверка улики сама сломана "
    "(сеть/секрет/API — не «улики нет», а «возможность сломана») → "
    "эскалация владельцу (issue #120 + Telegram), задача не тронута.\n\n"
    "Прод-форма: фикстуры — реальные тела/списки файлов/check-runs PR #138, "
    "#177, #163 и реальные записи задач #18, #21, #78.\n"
)

_PR_253_PULL = {
    "number": 253,
    "head": {"ref": "agent/227-acceptance-stage-after-merge"},
    "body": _PR_253_BODY,
}

# Тело dependabot-PR #282: в тексте много #N (номера чужих PR из changelog
# pnpm/action-setup — #175, #186, #283…), ветка не agent/, декларации нет —
# задачи нет вовсе, это ожидаемый, а не ошибочный ответ.
_PR_282_PULL = {
    "number": 282,
    "head": {"ref": "dependabot/github_actions/pnpm/action-setup-6"},
    "body": (
        "Bumps pnpm/action-setup from 4 to 6.\n\n"
        "- fix: update pnpm to v11.19.0 (#283)\n"
        "- docs: Update README (#273)\n"
        "- Additional commits viewable in compare view (#175, #186, #199)\n"
    ),
}


def test_resolve_pr_task_prefers_branch_over_prose_mention():
    # Живой случай #253: ветка/декларация #227, в прозе #120 — резолвер
    # обязан вернуть 227, не 120 (класс #259, второй экземпляр #187/#195:
    # первое попавшееся число в тексте вместо реального источника).
    assert task_ref.resolve_pr_task(_PR_253_PULL) == 227


def test_resolve_pr_task_bot_pr_has_no_task():
    # dependabot: ветка не agent/, декларации нет — «задачи нет», не 283/273.
    assert task_ref.resolve_pr_task(_PR_282_PULL) is None


def test_resolve_pr_task_falls_back_to_declared_without_agent_branch():
    # Ручной PR без agent-ветки — декларация первой строкой остаётся рабочим
    # источником (в отличие от прозы).
    pull = {"head": {"ref": "fix/typo"}, "body": "#42\n\nОпечатка в доке."}
    assert task_ref.resolve_pr_task(pull) == 42


def test_resolve_pr_task_uses_closing_issue_when_no_branch_or_declaration():
    # Источник (б) — формальная связь GitHub, передаётся вызывающим кодом
    # (GraphQL closingIssuesReferences, недоступен из task_ref.py напрямую —
    # резолвер обязан оставаться чистым, без сети).
    pull = {"head": {"ref": "fix/typo"}, "body": "Опечатка, без декларации."}
    assert task_ref.resolve_pr_task(pull, closing_issue=99) == 99
    assert task_ref.resolve_pr_task(pull) is None


def test_resolve_pr_task_branch_wins_over_closing_issue():
    # Иерархия строгая: ветка (а) надёжнее формальной связи (б).
    assert task_ref.resolve_pr_task(_PR_253_PULL, closing_issue=999) == 227


def test_task_from_branch_matches_agent_convention_only():
    assert task_ref.task_from_branch("agent/227-acceptance-stage-after-merge") == 227
    assert task_ref.task_from_branch("agent/259-pr-task-resolver") == 259
    assert task_ref.task_from_branch("dependabot/github_actions/foo-1") is None
    assert task_ref.task_from_branch("fix/typo") is None
    assert task_ref.task_from_branch("") is None


# ── pr_task_candidates (#394) — прод-форма реальных PR этого репозитория ──
#
# Живой класс: задача закрыта раньше срока («закрытая задача не
# переоткрывается»), докрытие оформлено НОВОЙ узкой задачей, объявленной
# первой строкой тела — ветку переименовать нельзя (agent/<N>-slug создаётся
# один раз, scripts/git/task-branch). Тела сохранены дословно
# (`gh api repos/mytab0r/edge-harness/pulls/388 --jq .body`), снято
# 2026-09-06.

_PR_388_BODY = (
    "#391\n\n"
    "Related: #256 (закрыта акцептансом 2026-09-05 как «без наблюдаемого "
    "результата» по доковому PR #260 — код по пп.1-2 tasks.md на тот момент "
    "ещё не был смёржен; правило 2026-09-06: закрытая задача не "
    "переоткрывается, новая узкая #391 по фактическому содержимому).\n"
)
_PR_388_PULL = {
    "number": 388,
    "head": {"ref": "agent/256-task-rework-loop"},
    "body": _PR_388_BODY,
}


def test_pr_task_candidates_rework_supersession_branch_and_body_both_present():
    # Живой случай #388 (постановка #394): ветка называет закрытую #256,
    # тело объявляет открытую-преемницу #391 — оба узких источника обязаны
    # попасть в кандидатов, ветка первой.
    assert task_ref.pr_task_candidates(_PR_388_PULL) == [256, 391]


def test_pr_task_candidates_dedupes_when_branch_and_body_agree():
    # Обычный случай (без реворка): ветка и тело называют одну и ту же
    # задачу — кандидат один, не дублируется.
    assert task_ref.pr_task_candidates(_PR_253_PULL) == [227]


def test_pr_task_candidates_bot_pr_has_no_candidates():
    assert task_ref.pr_task_candidates(_PR_282_PULL) == []


def test_pr_task_candidates_body_only_without_agent_branch():
    pull = {"head": {"ref": "fix/typo"}, "body": "#42\n\nОпечатка в доке."}
    assert task_ref.pr_task_candidates(pull) == [42]


def test_pr_task_candidates_branch_only_without_declaration():
    pull = {"head": {"ref": "agent/700-shell-body-only"}, "body": "просто описание без номера"}
    assert task_ref.pr_task_candidates(pull) == [700]


# Мутация, которой доказан resolve_pr_task (#259, воспроизведена буквально
# при разработке — вывод до/после в отчёте PR): временно замени тело функции
# на `refs = sorted(set(extract_task_refs(pull.get("body") or ""))); return
# refs[0] if refs else None` (старая широкая семантика ai_review.py:353) —
# extract_task_refs(_PR_253_BODY) отдаёт {18, 21, 78, 120, 138, 163, 177,
# 227}, наименьший 18 — test_resolve_pr_task_prefers_branch_over_prose_mention
# краснеет с `AssertionError: assert 18 == 227`, и ещё три теста резолвера
# падают вместе с ним (bot PR получает 175 вместо None, closing_issue
# перестаёт быть источником). Верни иерархию источников — все снова зелёные.
