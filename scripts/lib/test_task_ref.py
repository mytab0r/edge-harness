#!/usr/bin/env python3
"""Тесты извлечения номера задачи из текста (scripts/lib/task_ref.py, #187, #195).

Класс #187: `f"#{n}" in text` / `line.split("#")` матчат подстрокой — `#18`
совпадает с `#180`, `#181`, `#5180`. Живой прогон orchestra 33570081734:
контракт спутал PR #185 (задача #182) с задачей #18.

Класс #195/#259 (резолвер «PR → задача»): любое упоминание номера в прозе
тела PR — не то же самое, что «эта задача принадлежит PR». Решение владельца
2026-09-06 (#394, второй заход после `pr_task_candidates`): единственный
источник — имя agent-ветки, тело PR для этого вопроса не читается вовсе
(ни первой строкой, ни любым другим способом). Историю декларации первой
строкой тела (#251, #312, `declared_tasks`/`declares_task`, снятых вместе с
этим решением) см. в git-истории task_ref.py и
openspec/changes/contract-task-from-branch/proposal.md.

Кейсы кормятся прод-формой тела PR, которая реально встречается в
репозитории.

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
    # Прод-форма тела PR: первая строка ровно "#<N>", дальше пояснительный
    # текст с другими номерами — это упоминания (references_task), не
    # источник резолвера «PR → задача» (тот теперь читает только ветку).
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


# ── Резолвер «PR → задача» (#259, #394) — прод-форма реальных PR ────────────
#
# Живой замер, который и породил задачу: ai_review.py:353 брал ЛЮБОЕ #N из
# прозы тела, сортировал по возрастанию и судил PR по первому попавшемуся
# открытому issue с меткой task — #253 судили по #120 (упомянут в прозе,
# «issue #120 + Telegram»), хотя ветка объявляет #227. Решение владельца
# 2026-09-06: единственный источник — имя ветки; тело не читается вовсе,
# даже как запасной путь.

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
# pnpm/action-setup — #175, #186, #283…), ветка не agent/ — задачи нет
# вовсе, это ожидаемый, а не ошибочный ответ.
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

# Живой случай #388 (реворк): ветка называет уже ЗАКРЫТУЮ задачу #256, тело
# первой строкой объявляет открытую-преемницу #391. До решения владельца
# 2026-09-06 резолвер брал бы #391 из тела (`pr_task_candidates`) — теперь
# тело не читается вовсе, и резолвер обязан вернуть #256 (задачу ветки), а
# не переориентироваться неявно.
_PR_388_PULL = {
    "number": 388,
    "head": {"ref": "agent/256-task-rework-loop"},
    "body": (
        "#391\n\n"
        "Related: #256 (закрыта акцептансом 2026-09-05 как «без наблюдаемого "
        "результата»; правило: закрытая задача не переоткрывается, новая "
        "узкая #391 по фактическому содержимому).\n"
    ),
}


def test_resolve_pr_task_prefers_branch_over_prose_mention():
    # Живой случай #253: ветка #227, в прозе #120 — резолвер обязан вернуть
    # 227, не 120 (класс #259, второй экземпляр #187/#195: первое попавшееся
    # число в тексте вместо реального источника).
    assert task_ref.resolve_pr_task(_PR_253_PULL) == 227


def test_resolve_pr_task_bot_pr_has_no_task():
    # dependabot: ветка не agent/ — «задачи нет», не 283/273.
    assert task_ref.resolve_pr_task(_PR_282_PULL) is None


def test_resolve_pr_task_ignores_body_without_agent_branch():
    # Ручной PR без agent-ветки — решение владельца 2026-09-06: тело больше
    # не запасной путь, ответ «задачи нет», даже если первая строка тела
    # называет номер.
    pull = {"head": {"ref": "fix/typo"}, "body": "#42\n\nОпечатка в доке."}
    assert task_ref.resolve_pr_task(pull) is None


def test_resolve_pr_task_does_not_reorient_via_body_when_branch_task_closed():
    # Живой случай #388: ветка называет закрытую #256, тело объявляет
    # открытую #391 — резолвер обязан вернуть 256 (задачу ветки), не 391.
    # Переориентация чтением тела отменена без исключений.
    assert task_ref.resolve_pr_task(_PR_388_PULL) == 256


def test_task_from_branch_matches_agent_convention_only():
    assert task_ref.task_from_branch("agent/227-acceptance-stage-after-merge") == 227
    assert task_ref.task_from_branch("agent/259-pr-task-resolver") == 259
    assert task_ref.task_from_branch("dependabot/github_actions/foo-1") is None
    assert task_ref.task_from_branch("fix/typo") is None
    assert task_ref.task_from_branch("") is None


# ── closing_keyword_refs (#423) — прод-форма реальных тел PR #415 ───────────
#
# Обе строки сохранены дословно (`gh api graphql` — userContentEdits тела
# PR #415, edit 2026-09-06T02:46:33Z для v1 и 02:46:57Z для v2; полное тело
# восстановлено вокруг этих абзацев — контракт читает ВСЁ тело, не одну
# строку).

_PR_415_BODY_V1_LITERAL_DIRECTIVE = (
    "#413\n\n"
    "## Проблема\n\n"
    "`scripts/worker/task.sh` считал успехом прогона только ОТКРЫТЫЙ PR.\n\n"
    "## Чем доказано\n\n"
    "- `python -m pytest` — 12/12.\n\n"
    "Closes #413 руками не пишу (контракт запрещает `Closes/Fixes/Resolves` в\n"
    "теле PR) — issue закрою отдельно после мержа.\n"
)

_PR_415_BODY_V2_FIXED = (
    "#413\n\n"
    "## Проблема\n\n"
    "`scripts/worker/task.sh` считал успехом прогона только ОТКРЫТЫЙ PR.\n\n"
    "## Чем доказано\n\n"
    "- `python -m pytest` — 12/12.\n\n"
    "Слово «закрывает issue» намеренно не пишу ключевым словом GitHub (контракт\n"
    "`contract_check.py` отклоняет Closes/Fixes/Resolves в теле PR) — #413\n"
    "закрою отдельным комментарием после мержа, с уликами.\n"
)


def test_real_pr_415_literal_closes_is_a_real_directive():
    # Живой прогон `contract` 02:46:46 (2026-09-06) красил ИМЕННО эту версию
    # тела: голое «Closes #413» вне inline-кода — GitHub рендерит #413
    # ссылкой рядом со словом Closes (проверено `gh api markdown`, см.
    # докстринг task_ref.closing_keyword_refs) — настоящая директива,
    # контракт был прав.
    refs = task_ref.closing_keyword_refs(_PR_415_BODY_V1_LITERAL_DIRECTIVE)
    assert refs == ["Closes #413"]


def test_real_pr_415_fixed_body_explains_rule_not_directive():
    # После правки (перенос Closes/Fixes/Resolves в inline-код, голый номер
    # вынесен отдельной строкой в «#413») — то же тело красить нельзя: слова
    # находятся внутри `` ` `` — GitHub их директивой не считает (проверено
    # тем же рендерером). Старый код (`line.lstrip().lower().startswith(...)`)
    # эту конкретную строку не красил (см. репро в задаче #423), но не по
    # семантике, а по случайности — строка начиналась с обратной кавычки, не
    # со слова; тест фиксирует правильную причину, не совпадение позиции.
    refs = task_ref.closing_keyword_refs(_PR_415_BODY_V2_FIXED)
    assert refs == []


def test_closing_keyword_not_anchored_to_line_start():
    # Старая проверка (`startswith`) пропускала настоящую директиву не в
    # начале строки — «Этот PR closes #10 наконец» GitHub закрыл бы issue
    # при мерже, но contract этого не видел (второй дефект того же класса).
    assert task_ref.closing_keyword_refs("Этот PR closes #10 наконец.") == ["closes #10"]


def test_closing_keyword_ignored_inside_inline_code():
    assert task_ref.closing_keyword_refs("см. правило: `Closes #10` — пример") == []


def test_closing_keyword_ignored_inside_fenced_code_block():
    body = "текст\n```\nCloses #10\n```\nхвост"
    assert task_ref.closing_keyword_refs(body) == []


def test_closing_keyword_ignored_inside_html_comment():
    assert task_ref.closing_keyword_refs("<!-- Closes #10 -->") == []


def test_closing_keyword_html_comment_literal_inside_code_span_does_not_leak_next_span():
    # Живой ложноположительный случай (находка ревью #439): тело самого PR
    # #429 нарисовало `<!-- Closes #413 -->` буквальным ПРИМЕРОМ ВНУТРИ
    # инлайн-код-спана, а не настоящим HTML-комментарием. Старый порядок
    # (HTML-комментарий вырезается ДО код-спанов) резал текст комментария
    # даже внутри бэктиков, оставляя пустую пару `` `` `` — она не матчится
    # _INLINE_CODE_RE (нужен хотя бы один символ контента), один бэктик из
    # пары оставался бесхозным и ложно закрывал СЛЕДУЮЩИЙ реальный код-спан
    # дальше по тексту, освобождая директиву внутри него в прозу.
    text = (
        "HTML-комментарий (`<!-- Closes #413 -->`) — рендер вырезает,\n"
        "блокцитата (`> Closes #413`) — ссылка ЕСТЬ"
    )
    assert task_ref.closing_keyword_refs(text) == []


def test_closing_keyword_still_matches_inside_blockquote():
    # GitHub НЕ исключает блокцитаты из разбора директив (проверено рендером:
    # `> Closes #10` рендерится ссылкой) — в отличие от кода/комментария.
    assert task_ref.closing_keyword_refs("> Closes #10") == ["Closes #10"]


def test_closing_keyword_empty_text():
    assert task_ref.closing_keyword_refs("") == []
    assert task_ref.closing_keyword_refs(None) == []


def test_closing_keyword_crosses_soft_line_break():
    # Находка ревью #429, п.2: проверено рендерером GitHub (`gh api markdown`,
    # 2026-09-06) — "Closes\n#413" (мягкий перенос, без пустой строки между)
    # рендерится ОДНИМ <p>, #413 становится ссылкой — директива пересекает
    # перенос строки, contract обязан её ловить, не только «в одну строку».
    # Фрагмент возвращается со схлопнутым пробелом — без \n внутри, иначе
    # аннотация ::error:: и комментарий на PR ломаются форматированием.
    assert task_ref.closing_keyword_refs("Closes\n#413") == ["Closes #413"]


def test_closing_keyword_ignored_inside_multiline_inline_code_soft_break():
    # Находка ревью #429, п.2: "`Closes\n#10`" (мягкий перенос ВНУТРИ спана,
    # без пустой строки) — проверено рендерером: рендерится ОДНИМ
    # <code>Closes #10</code>, не директива. Старая _INLINE_CODE_RE (без
    # пересечения \n вовсе) эту форму не вырезала бы, и текст утёк бы в
    # прозу директивой — ложноположительно.
    assert task_ref.closing_keyword_refs("правило `Closes\n#10` соблюдай") == []


def test_closing_keyword_matches_after_blank_line_breaks_inline_code_span():
    # Находка ревью #429, п.2, обратная сторона: "`Closes\n\n#10`" (ПУСТАЯ
    # строка внутри обратных кавычек) — проверено рендерером: GitHub рвёт
    # абзац на пустой строке раньше закрывающей кавычки, спан не образуется,
    # обратная кавычка остаётся буквальным символом, #10 — снова директива.
    # _INLINE_CODE_RE обязан пересекать ОДИНОЧНЫЙ перенос, но не пустую
    # строку — иначе эта форма ложноотрицательно ушла бы в вырез как «код».
    assert task_ref.closing_keyword_refs("Слово `Closes\n\n#10` конец") == ["Closes #10"]


def test_closing_keyword_mutation_startswith_regresses_on_pr_415_v1():
    # Мутация: замени closing_keyword_refs на старую форму contract_check.py
    # (`[l for l in text.splitlines() if l.lstrip().lower().startswith(...)]`)
    # — этот тест по-прежнему проходит (v1 матчит и по старой логике), но
    # test_real_pr_415_fixed_body_explains_rule_not_directive СВАЛИТСЯ:
    # старая логика по СЛУЧАЙНОСТИ не красит v2 (строка начинается с обратной
    # кавычки), но красит любую вариацию, где текст начинается со слова — форма
    # ниже воспроизводит это ложноположительное срабатывание старой логики
    # напрямую (не полагаясь на случайность позиции обратной кавычки в v2);
    # проводку через contract_check.py на этой же форме держит отдельный тест
    # test_contract_check.py::test_false_positive_prose_about_the_rule_passes_contract
    # (находка ревью #429 — гвардия функции не гвардирует место вызова).
    def old_broken(text):
        return [
            line for line in text.splitlines()
            if line.lstrip().lower().startswith(("closes", "fixes", "resolves"))
        ]

    assert old_broken(_PR_415_BODY_V1_LITERAL_DIRECTIVE) != []
    # Старая логика красит объяснение правила, если оно случайно начинает
    # строку словом Closes/Fixes/Resolves — вот форма, где она ложноположительна,
    # а новая (task_ref.closing_keyword_refs) — нет:
    false_positive_body = "Closes/Fixes/Resolves запрещены контрактом — не пиши их."
    assert old_broken(false_positive_body) != [], "мутация должна воспроизводить старый баг"
    assert task_ref.closing_keyword_refs(false_positive_body) == [], (
        "не директива: после ключевого слова нет #N вплотную — GitHub тут "
        "ничего не закроет, красить нечего"
    )


# ── also_closes_targets (#1042) — прод-форма реальных тел трёх слитых PR ──────
#
# Все три взяты дословно из тел уже слитых PR этого репозитория (проверено
# `gh pr view <N> --json body`, 2026-09-12), не пересказаны.

_PR_986_FRAGMENT = (
    "- Заявленные в issue #507 упоминания `dsh-tools@0.1.1-rc.2` в\n"
    "  README/body.js сверены отдельно и не относятся к зависимостям,\n"
    "  правкой; закрываю issue #507 этим PR как полностью покрытый.\n"
)

_PR_986_FALSE_POSITIVE_FRAGMENT = (
    "в докстринге roster-гвардии — сейчас цепочка, от которой зависит "
    "закрытие #806, живёт только в переносе"
)

_PR_986_NEGATION_FRAGMENT = (
    "- #806 — причина 2 (ростер), не закрывается автоматически этим PR: "
    "реальный перенос требует отдельного PR."
)

_PR_952_FRAGMENT = (
    "форж отказывает\nгромко ДО сборки, если номер не определён "
    "(закрывает #661, вариант 1).\n"
)

_PR_841_FRAGMENT = (
    "- Закрыт класс #786 по всему `scripts/`: `gh` 2.85 не знает "
    "`--body-file` у `gh secret set`.\n"
)


def test_also_closes_targets_real_pr_986_declares_issue_507():
    assert task_ref.also_closes_targets(_PR_986_FRAGMENT) == [507]


def test_also_closes_targets_real_pr_952_declares_issue_661():
    assert task_ref.also_closes_targets(_PR_952_FRAGMENT) == [661]


def test_also_closes_targets_real_pr_841_declares_issue_786_with_class_word():
    assert task_ref.also_closes_targets(_PR_841_FRAGMENT) == [786]


def test_also_closes_targets_rejects_noun_form():
    # Живой ложноположительный случай (тело PR #986, 2026-09-12): «закрытие»
    # — существительное, не одна из глагольных форм списка. Тот же PR прямо
    # пишет рядом, что #806 НЕ закрывается им (см. следующий тест) — открытый
    # корень `закры[а-я]*` (первая попытка регэкспа) матчил бы оба фрагмента
    # неразличимо; список конкретных форм отклоняет именно этот.
    assert task_ref.also_closes_targets(_PR_986_FALSE_POSITIVE_FRAGMENT) == []


def test_also_closes_targets_ignores_number_before_verb():
    # «#806 — … не закрывается автоматически этим PR» — номер стоит ДО
    # глагола, регэксп ищет номер СРАЗУ ПОСЛЕ глагола (см. докстринг
    # _ALSO_CLOSES_RE) и это отрицание структурно не матчит.
    assert task_ref.also_closes_targets(_PR_986_NEGATION_FRAGMENT) == []


def test_also_closes_targets_mutation_open_root_regresses_on_986():
    # Мутация: замени явный список форм на открытый корень (первая версия,
    # отвергнутая экспериментом) — воспроизводим её здесь напрямую, не
    # полагаясь на то, что кто-то повторит ту же правку в исходнике.
    import re
    open_root_re = re.compile(
        r"(?i)\bзакры[а-яё]*\b(?:\s+(?:issue|класс))?\s*#(\d+)"
    )
    matches = [int(m.group(1)) for m in open_root_re.finditer(_PR_986_FALSE_POSITIVE_FRAGMENT)]
    assert matches == [806], "мутация (открытый корень) обязана воспроизводить ложное срабатывание на #806"
    # А явный список форм (актуальный код) — нет:
    assert task_ref.also_closes_targets(_PR_986_FALSE_POSITIVE_FRAGMENT) == []


def test_also_closes_targets_empty_text():
    assert task_ref.also_closes_targets("") == []
    assert task_ref.also_closes_targets(None) == []


def test_also_closes_targets_dedupes_preserving_order():
    text = "закрывает #10, а также закрывает #20 и снова закрывает #10."
    assert task_ref.also_closes_targets(text) == [10, 20]


def test_also_closes_targets_does_not_match_english_closing_directive():
    # Русские глагольные формы не входят в список ключевых слов GitHub —
    # `also_closes_targets` не пересекается с closing_keyword_refs (разные
    # языки, разный признак), но проверяем явно: английская директива сама
    # по себе не матчит русский маркер.
    assert task_ref.also_closes_targets("Closes #413") == []


# Мутация, которой доказан resolve_pr_task (#259, #394): временно замени тело
# функции на `refs = sorted(set(extract_task_refs(pull.get("body") or "")));
# return refs[0] if refs else None` (старая широкая семантика ai_review.py:353)
# — extract_task_refs(_PR_253_BODY) отдаёт {18, 21, 78, 120, 138, 163, 177,
# 227}, наименьший 18 — test_resolve_pr_task_prefers_branch_over_prose_mention
# краснеет с `AssertionError: assert 18 == 227`. Отдельно: верни чтение тела
# (`declared = extract_task_refs(...); return from_branch or (declared[0] if
# declared else None)`) — test_resolve_pr_task_ignores_body_without_agent_branch
# и test_resolve_pr_task_does_not_reorient_via_body_when_branch_task_closed
# краснеют (вернётся 42/391 вместо None/256). Верни чистую `task_from_branch` —
# все снова зелёные.
