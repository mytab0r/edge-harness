#!/usr/bin/env python3
"""Тесты гвардии «протухшей» метки blocked (scripts/orchestra/stale_blocked_guard.py, #334).

Кормятся прод-формой:
- тело и эскалационный комментарий issue #268 сняты живым
  `gh issue view 268 --json body,comments` 2026-09-05 — на тот момент #268
  несёт метку `blocked` и в комментарии буквально называет причину маркером
  «Блокирована: #265»; #265 закрыт 2026-09-05T13:33:09Z
  (`gh issue view 265 --json state,closedAt`). Это случай, который гвардия
  обязана поймать.
- тело issue #216 (тем же способом, 2026-09-05) — тоже несёт метку `blocked`,
  но называет ТРИ номера союзом «И» без маркера («PR #162 … #163 … #164»);
  #163/#164 закрыты, #162 ещё открыт на момент снятия. Первая версия признака
  (task_ref.extract_task_refs — любое упоминание #N) на этих же живых данных
  дала бы ложное срабатывание: пометила бы #216 протухшей, хотя блокировка
  законна, пока открыт #162. Тест ниже доказывает, что суженный маркерный
  признак этого не делает.

Проводка stale_blocked_check — на моке pulse_guard.gh (единственный пункт
патча, как у upstream_drift_check), сеть не нужна.

Запуск: python -m pytest scripts/orchestra/test_stale_blocked_guard.py -q
"""

import importlib.util
import sys
from pathlib import Path

import pytest

_DIR = Path(__file__).resolve().parent
if str(_DIR) not in sys.path:
    sys.path.insert(0, str(_DIR))

SCRIPT = _DIR / "stale_blocked_guard.py"
spec = importlib.util.spec_from_file_location("stale_blocked_guard", SCRIPT)
sbg = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sbg)  # type: ignore[union-attr]

pg = sys.modules["pulse_guard"]

REPO = "mytab0r/edge-harness"

# ── Прод-форма: тело и комментарий #268 (сняты 2026-09-05 живым gh) ─────────────

ISSUE_268_BODY = (
    "## Цель\n\nРеализовать `.github/workflows/plugin-forge.yml` — форж плагинов "
    "в раннере (эпик #77, третья стройка «Форж и канал обновления», "
    "`openspec/changes/dsh-edge-plugin-system/design.md`).\n\n"
    "## Зависимости\n\nЗависит от #265 (`check-plugin-compat.mjs`) — форж использует "
    "чекер как обязательный первый гейт, без него неоткуда взять шаг совместимости.\n"
)

ISSUE_268_ESCALATION_COMMENT = (
    "Блокирована: #265\n\n"
    "Форж плагинов использует `check-plugin-compat.mjs` как обязательный первый гейт "
    "совместимости. Без #265 нечего выполнять в гейте.\n\n"
    "Газ: слияние PR #265 (и переход в open state в исходной задаче #265) — проверить "
    "через `gh issue view 265 --json state`."
)

# Прод-форма: комментарий #215/#258 (сняты живым `gh issue view 215/258
# --comments` при разборе issue #938, дословно) — вторая живая формулировка
# маркера, «Причина блокировки: #N» вместо «Блокирована: #N». #809 закрыт
# 2026-09-09; метка `blocked` на #215/#258 не снята никем, потому что старый
# STALE_MARKER_RE эту формулировку не узнавал.
ISSUE_215_ESCALATION_COMMENT = (
    "🔒 Выведена из ротации (blocked): сессия `harness-215` испорчена багом "
    "#794 (событие журнала без message.id → морда роняет холодную загрузку). "
    "PR #795 остановил порчу НОВЫХ сессий, но эту не воскрешает — воркер "
    "падал бы на ней при каждом ходе и крутил предохранитель диспатча. Газ: "
    "снять blocked, когда закрыт класс-фикс #809 (воркер/морда "
    "восстанавливаются при битой загрузке) или сессия восстановлена. "
    "Причина блокировки: #809."
)


def issue_215(labels=("task", "white-spot", "blocked", "stale-unclaimed")):
    return {
        "number": 215,
        "labels": [{"name": name} for name in labels],
        "body": "тело #215 не участвует в этом тесте",
        "comments_text": [ISSUE_215_ESCALATION_COMMENT],
    }


# Прод-форма: тело issue #216 (сняты 2026-09-05 живым gh) — три номера союзом
# «И», БЕЗ маркера «Блокирована: #N». #162 открыт, #163/#164 закрыты на
# момент снятия — легитимный «живой» кандидат на ложное срабатывание.
ISSUE_216_BODY = (
    "## Зачем\n\nПровайдеры падают перемежающимися сбоями. Решение владельца "
    "2026-09-02: плагин пишем и деплоим мы сами, внешнего артефакта нет. "
    "Подробности — в #215.\n\n"
    "## Блокирующая зависимость\n\nСобрать и задеплоить плагин нечем, пока не "
    "слита плагинная машинерия эпика #77: PR #162 (plugin-forge), #163 "
    "(дизайн), #164 (PoC hello-world через forge → deploy → UI). Это "
    "зависимость, а не пожелание — сначала машинерия, потом плагин.\n"
)


def issue_268(labels=("task", "area:worker", "blocked")):
    return {
        "number": 268,
        "labels": [{"name": name} for name in labels],
        "body": ISSUE_268_BODY,
        "comments_text": [
            "♻️ Задача возвращена в пул оркестратором: PR #273 нездоров дольше 120 мин.",
            ISSUE_268_ESCALATION_COMMENT,
        ],
    }


def issue_216():
    return {
        "number": 216,
        "labels": [{"name": "task"}, {"name": "blocked"}],
        "body": ISSUE_216_BODY,
        "comments_text": [],
    }


# ── Чистое решение ────────────────────────────────────────────────────────────


def test_stale_marker_targets_finds_only_marked_number_in_268():
    texts = [ISSUE_268_BODY, ISSUE_268_ESCALATION_COMMENT]
    # #77 и «Зависит от #265» в теле — БЕЗ маркера «Блокирована:», не считаются;
    # ровно один номер приходит из маркера в комментарии.
    assert sbg.stale_marker_targets(268, texts) == [265]


def test_stale_marker_targets_finds_nothing_in_216_prose_conjunction():
    """Живой контрпример: #216 называет #162/#163/#164 союзом «И», без
    маркера «Блокирована: #N» — признак не срабатывает вовсе."""
    assert sbg.stale_marker_targets(216, [ISSUE_216_BODY]) == []


def test_stale_marker_targets_excludes_self_and_dedupes():
    texts = ["Блокирована: #265\n…\nБлокирована: #265", "Блокирована: #268"]
    assert sbg.stale_marker_targets(268, texts) == [265]


def test_stale_marker_targets_ignores_marker_quoted_mid_prose():
    """Находка ревью PR #336: маркер обязан стоять в НАЧАЛЕ строки. Цитата
    старого маркера посреди прозы (пересказ/обсуждение) — не новая причина,
    иначе гвардия сама сеет ложное срабатывание своим же эскалационным
    текстом (violation_text цитирует маркер)."""
    texts = ['Обсуждаем старый эпизод: было «Блокирована: #265», уже решено']
    assert sbg.stale_marker_targets(268, texts) == []


def test_stale_marker_targets_ignores_marker_inside_word():
    """Находка ревью PR #336: «заблокирована: #N» (маркер — суффикс другого
    слова, не начало строки) не считается названной причиной."""
    texts = ["задача заблокирована: #164"]
    assert sbg.stale_marker_targets(268, texts) == []


def test_stale_marker_targets_ignores_quoted_list_item_at_line_start():
    """Блокирующая находка ревью PR #1216 (второй раунд), класс #1162/#1121:
    ЦИТАТА чужой строки зависимости списочным элементом в начале строки в
    КОММЕНТАРИИ помеченной issue («- Заблокировано #809 (…) ← цитата из
    чужого тела») не становится «текущей причиной» метки — иначе гвардия
    сама снимает живой тормоз по чужому номеру. Строгий STALE_MARKER_RE
    списочный префикс и ведущие пробелы не допускает.

    МУТАЦИЯ: `current_stale_marker_target`/`stale_marker_targets` читают
    расширенный POOL_MARKER_RE вместо строгого STALE_MARKER_RE (возврат
    утечки) — тест краснеет (цитата матчится); мутация снята."""
    texts = [
        "Легитимный контекст: обсудили зависимость.\n"
        "- Заблокировано #809 (цитата из чужого тела, не наше объявление)\n"
        "  Причина блокировки: #809 (цитата с отступом)",
    ]
    assert sbg.stale_marker_targets(268, texts) == []
    assert sbg.current_stale_marker_target(268, texts) is None
    # Зеркало: та же строка как ТЕЛО помеченной issue — тоже не причина.
    assert sbg.current_stale_marker_target(268, [texts[0]]) is None


def test_stale_marker_targets_strict_still_reads_live_escalation_forms():
    """Сужение якоря не сломало живые формы помеченного пути: прод-форма
    #268 (начало строки) и #215 («. Причина блокировки: #809.» после
    точки-пробела) читаются строгим регэкспом как раньше (#1157 не
    регрессировала)."""
    assert sbg.current_stale_marker_target(268, [ISSUE_268_BODY,
                                                  ISSUE_268_ESCALATION_COMMENT]) == 265
    assert sbg.current_stale_marker_target(215, [ISSUE_215_ESCALATION_COMMENT]) == 809


def test_stale_marker_targets_finds_marker_in_alternate_prichina_blokirovki_form():
    """Живой случай #215/#258 (issue #938): «Причина блокировки: #N» — вторая
    формулировка того же маркера, не только «Блокирована: #N»."""
    assert sbg.stale_marker_targets(215, [ISSUE_215_ESCALATION_COMMENT]) == [809]


def test_find_stale_blocked_flags_215_on_prod_form_with_809_closed():
    """Мутация: #809 закрыт (прод-факт, issue #938) — #215 обязана попасть в
    отчёт, несмотря на альтернативную формулировку маркера."""
    violations = sbg.find_stale_blocked([issue_215()], closed_numbers={809})
    assert violations == [{"number": 215, "stale_refs": [809]}]


def test_find_stale_blocked_silent_while_809_still_open():
    violations = sbg.find_stale_blocked([issue_215()], closed_numbers=set())
    assert violations == []


def test_find_stale_blocked_flags_268_on_prod_form_with_265_closed():
    """Мутация (а): #265 закрыт (прод-форма факта) — #268 обязана попасть в отчёт."""
    violations = sbg.find_stale_blocked([issue_268()], closed_numbers={265})
    assert violations == [{"number": 268, "stale_refs": [265]}]


def test_find_stale_blocked_silent_while_265_still_open():
    """Мутация (б): та же #268, но #265 ещё открыт — нарушения нет (условие
    специфично к состоянию ссылки, не к самому факту ссылки)."""
    violations = sbg.find_stale_blocked([issue_268()], closed_numbers=set())
    assert violations == []


def test_find_stale_blocked_ignores_issue_without_blocked_label():
    issue = issue_268(labels=("task", "area:worker"))
    violations = sbg.find_stale_blocked([issue], closed_numbers={265})
    assert violations == []


def test_find_stale_blocked_no_false_positive_on_216_even_with_163_164_closed():
    """Живой класс ложного срабатывания: #163 и #164 закрыты, #162 (не
    маркированный, союз «И» в прозе) ещё открыт — #216 НЕ попадает в отчёт,
    хотя закрытые номера присутствуют в closed_numbers."""
    violations = sbg.find_stale_blocked([issue_216()], closed_numbers={163, 164})
    assert violations == []


def test_find_stale_blocked_silent_on_resolved_episode_after_legit_reblock():
    """Находка AI-ревью PR #336 (третий раунд), живой контрпример на
    сгенерированном моке (реальный #268 на сегодня несёт только один эпизод —
    этот сценарий гипотетический, но структурно неизбежен при первом же
    resolve→re-block): эпизод «Блокирована: #265» решён (#265 закрыт), метку
    сняли и снова поставили с НОВЫМ легитимным эскалационным комментарием
    «Блокирована: #300» (#300 ещё открыт). Старая семантика («любое
    упоминание в истории — нарушение, если номер закрыт») продолжала бы
    видеть #265 и красить шаг каждый прогон; правильное поведение — молчать,
    текущая причина (#300) легитимна."""
    issue = {
        "number": 268,
        "labels": [{"name": "task"}, {"name": "blocked"}],
        "body": ISSUE_268_BODY,
        "comments_text": [
            ISSUE_268_ESCALATION_COMMENT,  # старый эпизод: «Блокирована: #265»
            "Метка blocked снята владельцем — #265 слит.",
            "Блокирована: #300\n\nНовый эпизод: задача снова заблокирована "
            "на #300, ещё не готов.",
        ],
    }
    # #265 закрыт (старый эпизод), #300 ещё открыт (текущий) — старая
    # семантика нашла бы #265 в истории и ложно сработала.
    violations = sbg.find_stale_blocked([issue], closed_numbers={265})
    assert violations == []


def test_find_stale_blocked_flags_current_marker_when_reblock_target_closed():
    """Зеркало предыдущего теста: если ТЕКУЩИЙ (последний) маркер указывает
    на уже закрытый номер — нарушение есть, несмотря на более раннюю (и не
    закрытую) историю."""
    issue = {
        "number": 268,
        "labels": [{"name": "task"}, {"name": "blocked"}],
        "body": ISSUE_268_BODY,
        "comments_text": [
            "Блокирована: #400\n\nПервый эпизод, #400 ещё открыт на тот момент.",
            "Метка blocked снята владельцем — #400 слит.",
            ISSUE_268_ESCALATION_COMMENT,  # текущий эпизод: «Блокирована: #265»
        ],
    }
    violations = sbg.find_stale_blocked([issue], closed_numbers={265})
    assert violations == [{"number": 268, "stale_refs": [265]}]


def test_current_stale_marker_target_picks_last_not_first():
    texts = ["Блокирована: #100", "не маркер, просто текст", "Блокирована: #200"]
    assert sbg.current_stale_marker_target(268, texts) == 200


def test_find_stale_blocked_silent_on_blocked_issue_without_any_reference():
    """Законный ручной случай (LABELS.md): эскалация «нужен секрет владельца»
    без ссылки на другой issue — не флагуется, это не машинно проверяемо."""
    issue = {
        "number": 6,
        "labels": [{"name": "task"}, {"name": "blocked"}],
        "body": "## Цель\nУзкий PAT для dispatch.",
        "comments_text": ["Упёрся в то, что есть только у владельца: fine-grained PAT "
                           "создаётся только через веб-интерфейс."],
    }
    violations = sbg.find_stale_blocked([issue], closed_numbers={265})
    assert violations == []


def test_violation_text_names_issue_and_stale_refs():
    text = sbg.violation_text({"number": 268, "stale_refs": [265]})
    assert "#268" in text and "#265" in text and "blocked" in text


# ── Чистое решение: маркер эскалации (находка ревью #333/#336) ─────────────────


def test_escalation_target_is_stable_sorted_refs():
    assert sbg.escalation_target({"number": 268, "stale_refs": [265]}) == "#265"
    assert sbg.escalation_target({"number": 1, "stale_refs": [3, 5]}) == "#3,#5"


def test_escalation_marker_target_reads_body():
    body = "🚨 [протухшая блокировка: #265]\nтекст"
    assert sbg.escalation_marker_target(body) == "#265"


def test_escalation_marker_target_none_without_marker():
    assert sbg.escalation_marker_target("обычный комментарий без маркера") is None


def test_already_escalated_true_for_same_target():
    texts = ["шум", "🚨 [протухшая блокировка: #265]\nсигнал"]
    assert sbg.already_escalated(texts, "#265") is True


def test_already_escalated_false_for_different_target():
    """Набор протухших ссылок сменился (новая ссылка протухла/старая ушла) —
    это новый эпизод, старый маркер не гасит новый сигнал."""
    texts = ["🚨 [протухшая блокировка: #163]\nсигнал"]
    assert sbg.already_escalated(texts, "#265") is False


def test_escalation_text_puts_marker_first_line():
    text = sbg.escalation_text({"number": 268, "stale_refs": [265]})
    assert text.startswith("🚨 [протухшая блокировка: #265]")
    assert "#268" in text


# ══════════════════════════════════════════════════════════════════════════
# Issue #1211: объявление блокера БЕЗ метки `blocked` — прочёс всего пула.
# Прод-форма: тела сняты живым `gh issue view <N> --json body` 2026-09-14
# (см. отчёт по #1211) с реального открытого пула (378 issue на тот момент).
# ══════════════════════════════════════════════════════════════════════════

# Инлайн-конвенция «БЛОКИРУЕТСЯ: #N» последней строкой — прод-форма #1016.
ISSUE_1016_BODY = (
    "Транслятор срабатывает только на механическом ребейзе (метка `conflict`), поэтому "
    "исходная очередь из замера (14 PR / 20 шагов) сама не рассосётся: PR без конфликта так "
    "и останутся красными по `ci_guard_registration`. Нужен разовый прогон транслятора по "
    "открытым PR (или точечные ребейзы), критерий готовности: 0 открытых PR с рукописным "
    "шагом гвардии вне ALLOWLIST.\n"
    "БЛОКИРУЕТСЯ: #897\n"
)

# Та же конвенция — прод-форма #976 (второй живой пример, другой номер).
ISSUE_976_BODY = (
    "Design сознательно откладывает перенос остальных правил «после того, как скелет "
    "подтвердит цикл живым прогоном CI» — оформи это задачей пула, чтобы хвост не испарился.\n"
    "БЛОКИРУЕТСЯ: #648\n"
)

# «Заблокировано #N» БЕЗ двоеточия, внутри маркдаун-списка под заголовком —
# прод-форма #86.
ISSUE_86_BODY = (
    "## Цель\nПо курсу эпика #77 (вся логика на dsh-edge) старый воркер edge-harness — "
    "временное транспортное плечо, не постоянный житель. После PoC плагинной системы (#80) "
    "перевезти его функции в dsh-edge плагином (hands-bridge) и удалить воркер из CF.\n\n"
    "## Критерий готовности\n- Весь цикл (задача → раннер → статусы в чате dsh-edge) работает "
    "без edge-harness воркера; воркер удалён.\n\n"
    "## Зависимости\n- Заблокировано #80 (плагинная система PoC).\n"
)

# Структурное поле формы «### Чем блокируется», ЧИСТЫЙ ответ (один номер) —
# прод-форма #717.
ISSUE_717_BODY = (
    "### Цель\n\nПодтвердить живым прогоном такт Cloudflare Cron Trigger.\n\n"
    "### Площадь\n\narea:orchestra\n\n"
    "### Чем блокируется\n\n#713\n\n"
    "### Что блокирует\n\nничем\n"
)

# То же структурное поле — прод-форма #741 (другой номер, доказывает, что
# признак не завязан на конкретное число).
ISSUE_741_BODY = (
    "### Цель\n\nНеуспешный прогон второго гейта не понижает уже выданный зелёный вердикт.\n\n"
    "### Площадь\n\narea:orchestra\n\n"
    "### Чем блокируется\n\n#740\n\n"
    "### Что блокирует\n\nничем\n"
)

# Живой контрпример №1 (issue #1215, найден при разборе #1211): поле отвечает
# «ничего (…)», а В СКОБКАХ поясняет номера, которые автор ПРЯМО называет НЕ
# блокирующими. declared_deps.form_field_numbers вытянул бы [361, 286, 543]
# как «объявленные блокеры» — ложное срабатывание; structural_field_targets
# обязан вернуть "unrecognized", не список.
ISSUE_768_BODY = (
    "### Чем блокируется\n\n"
    "ничего (граф зависимостей #361 подхватит автоматически; #286 и #543 — смежные, "
    "не блокирующие)\n"
)

# Живой контрпример №2 — прод-форма #971: ответ не список номеров, а свободная
# фраза («PR #950 занят»), лишний текст вокруг числа.
ISSUE_971_BODY = (
    "### Чем блокируется\n\nPR #950 (scripts/orchestra/pulse_guard.py занят)\n\n"
)

# Живой контрпример №3 — прод-форма #1125: явный ответ «ничем», но с
# концевой точкой (человеческая пунктуация) — обязан читаться как ПУСТОЙ
# ответ («блокеров нет»), не как unrecognized (issue #1215, дефект 1
# declared_deps.form_field_numbers, здесь НЕ воспроизводится).
ISSUE_1125_BODY = "### Чем блокируется\n\nНичем.\n"


def test_structural_field_targets_reads_clean_single_number_717():
    assert sbg.structural_field_targets(ISSUE_717_BODY) == ("declared", [713])


def test_structural_field_targets_reads_clean_single_number_741():
    assert sbg.structural_field_targets(ISSUE_741_BODY) == ("declared", [740])


def test_structural_field_targets_absent_without_header():
    assert sbg.structural_field_targets(ISSUE_86_BODY) == ("absent", None)


def test_structural_field_targets_rejects_explanation_after_nichego_768():
    """Мутация-доказанный живой ложный позитив (issue #1215): «ничего (текст
    с номерами, помеченными как НЕ блокирующие)» — не список, не пустой ответ,
    третий исход."""
    assert sbg.structural_field_targets(ISSUE_768_BODY) == ("unrecognized", None)


def test_structural_field_targets_rejects_free_prose_971():
    assert sbg.structural_field_targets(ISSUE_971_BODY) == ("unrecognized", None)


def test_structural_field_targets_accepts_nichem_with_trailing_dot_1125():
    """#1125: «Ничем.» — валидный пустой ответ, НЕ unrecognized (issue #1215,
    дефект 1 — здесь исправлен локально, declared_deps.py не правится)."""
    assert sbg.structural_field_targets(ISSUE_1125_BODY) == ("empty", [])


def test_declared_targets_inline_marker_beats_structural_field():
    """ЕДИНОЕ правило старшинства на оба пути (второе ревью PR #1216:
    «два разных правила старшинства в одном модуле»): разборный
    инлайн-маркер старше структурного поля и на непомеченной стороне —
    на этой поверхности он тоже самое свежее договорное высказывание
    (последняя строка тела), поле — ответ схемы при заведении. Тело с
    полем «#713» и последней строкой «Блокирована: #301» объявляет #301."""
    body = ISSUE_717_BODY + "\nБлокирована: #301\n"
    result = sbg.declared_targets(717, body)
    assert result == {"form": "inline", "targets": [301], "unrecognized": False}


def test_declared_targets_last_match_prefers_contract_line_over_quote():
    """Второе ревью PR #1216: «внутри тела берётся первый матч, хотя
    контрактная форма живёт последней строкой». Тело с ранней ЦИТАТОЙ
    чужого маркера списочным элементом (класс #1162/#1121) и договорной
    последней строкой объявляет ПОСЛЕДНИЙ номер, не цитату.

    МУТАЦИЯ: `last_inline_marker_target` с `finditer`-последний заменён на
    `POOL_MARKER_RE.search` (первый матч) — тест краснеет (цель [80], не
    [897]); мутация снята."""
    body = (
        "## Зависимости\n- Заблокировано #80 (устаревшая строка из чужого тела)\n\n"
        "Транслятор ждёт переноса гвардии в каталог scripts/ci/guards.\n"
        "БЛОКИРУЕТСЯ: #897\n"
    )
    result = sbg.declared_targets(1016, body)
    assert result == {"form": "inline", "targets": [897], "unrecognized": False}


def test_last_inline_marker_target_reads_list_form_without_colon_86():
    """Расширенный POOL_MARKER_RE читает прод-форму #86 — «- Заблокировано
    #80» без двоеточия, списочным элементом (СТРОГИЙ регэксп её не матчит —
    это и есть граница двух поверхностей)."""
    assert sbg.last_inline_marker_target(86, ISSUE_86_BODY) == 80
    assert sbg.current_stale_marker_target(86, [ISSUE_86_BODY]) is None


def test_last_inline_marker_target_ignores_self_reference():
    last = sbg.last_inline_marker_target(897, "БЛОКИРУЕТСЯ: #897\n")
    assert last is None


def test_declared_targets_falls_back_to_inline_marker_1016():
    result = sbg.declared_targets(1016, ISSUE_1016_BODY)
    assert result == {"form": "inline", "targets": [897], "unrecognized": False}


def test_declared_targets_finds_no_colon_form_86():
    result = sbg.declared_targets(86, ISSUE_86_BODY)
    assert result == {"form": "inline", "targets": [80], "unrecognized": False}


def test_declared_targets_flags_unrecognized_structural_768():
    result = sbg.declared_targets(768, ISSUE_768_BODY)
    assert result == {"form": "structural", "targets": None, "unrecognized": True}


def test_declared_targets_unrecognized_field_does_not_silence_inline_marker():
    """Второе ревью PR #1216 (старшинство источников): неузнаваемое поле
    НЕ глушит разборный инлайн-маркер. Тело с битым полем (#768) и живой
    последней строкой «БЛОКИРУЕТСЯ: #897» объявляет #897 (проверяется), а
    битость поля едет отдельно (❓) — молчаливой потери протухшего
    объявления за неразборчивой формой нет.

    МУТАЦИЯ: возврат к старому правилу в `declared_targets`
    (`if unrecognized: return {..., "targets": None, ...}` ПЕРЕД чтением
    инлайна) — тест краснеет (targets None); мутация снята."""
    body = ISSUE_768_BODY + "БЛОКИРУЕТСЯ: #897\n"
    result = sbg.declared_targets(768, body)
    assert result == {"form": "inline", "targets": [897], "unrecognized": True}


def test_declared_targets_none_when_body_has_neither_form():
    """#708 (проверено живьём при разборе #1211): исходная гипотеза ревизии
    «#708 заблокирован #500» не подтвердилась — тело не несёт ни структурного
    поля, ни инлайн-маркера, «#500» упомянут только в ЦИТАТЕ примера ДРУГОГО
    регэкспа внутри чек-листа, не как объявление блокировки этой задачи."""
    body_708 = (
        "PR #663 слит с незакрытыми пунктами чеклиста ревью.\n\n"
        "- [ ] якорь `^Задач` слишком широкий — под него попадает проза вида "
        "«Задачи, от которых зависит: #500» — и чужой номер станет «собственной задачей»."
    )
    result = sbg.declared_targets(708, body_708)
    assert result == {"form": None, "targets": None, "unrecognized": False}


def test_declared_targets_ignores_marker_quoted_in_review_checklist_1162():
    """#1162 (проверено живьём): «Блокирована: #809. … Причина блокировки:
    #900.» встречается ТОЛЬКО внутри цитаты, обсуждающей баг самой этой
    гвардии (чек-лист ревью PR #1159) — позиционный якорь STALE_MARKER_RE
    (не после ««», не начало строки) не даёт ложного срабатывания."""
    body_1162 = (
        "PR #1159 слит с незакрытыми пунктами чеклиста.\n\n"
        "- [ ] два разных маркера в последнем тексте — Проверил на коде: текст "
        "«Блокирована: #809. … Причина блокировки: #900.» "
        "(`current_stale_marker_target` берёт первый матч последнего текста) при "
        "закрытом #809 и открытом #900 даёт violation."
    )
    result = sbg.declared_targets(1162, body_1162)
    assert result == {"form": None, "targets": None, "unrecognized": False}


# ── target_status: третье состояние (issue #1211) ──────────────────────────


def test_target_status_closed(monkeypatch):
    monkeypatch.setattr(sbg, "issue_state",
                         lambda repo, n: {"state": "closed", "closed_at": "2026-09-12T10:26:27Z"})
    status, data = sbg.target_status(REPO, 897)
    assert status == "closed"
    assert data["closed_at"] == "2026-09-12T10:26:27Z"


def test_target_status_open(monkeypatch):
    monkeypatch.setattr(sbg, "issue_state", lambda repo, n: {"state": "open"})
    assert sbg.target_status(REPO, 448)[0] == "open"


def test_target_status_missing_on_real_gh_404_form(monkeypatch):
    """Прод-форма отказа gh на 404 (issue #1211, третий исход «не
    существует» — синтетический номер, ЖИВОГО случая объявления блокировки
    на несуществующий номер на пуле 2026-09-14 не нашлось: исходная гипотеза
    ревизии #708→#500 не подтвердилась (см. тест выше), #500 к тому же
    СУЩЕСТВУЕТ как PR — механизм всё равно обязан существовать для будущего
    живого случая, доказывается конструированной, честно помеченной формой)."""
    def raiser(repo, n):
        raise RuntimeError(f"gh api repos/{repo}/issues/{n}: gh: Not Found (HTTP 404)")
    monkeypatch.setattr(sbg, "issue_state", raiser)
    assert sbg.target_status(REPO, 999999) == ("missing", {})


def test_target_status_unknown_on_other_error(monkeypatch):
    """404 отличается от прочих отказов ТОЧНОЙ формой (`ai_review.is_not_found`,
    не подстрокой «404» — иначе номер issue С «404» в себе молча считался бы
    «не существует», находка ревью PR #711)."""
    def raiser(repo, n):
        raise RuntimeError("gh api repos/o/r/issues/1: Forbidden (HTTP 403)")
    monkeypatch.setattr(sbg, "issue_state", raiser)
    status, data = sbg.target_status(REPO, 1)
    assert status == "unknown"
    assert "403" in data["reason"]


def test_target_status_unknown_on_unexpected_shape(monkeypatch):
    """Ответ без `state` (неожиданная форма) — тоже "unknown", не молчаливое
    попадание ни в open, ни в closed (AGENTS.md «не смог прочитать» ≠ «связь
    цела»)."""
    monkeypatch.setattr(sbg, "issue_state", lambda repo, n: {})
    assert sbg.target_status(REPO, 1)[0] == "unknown"


# ── marker_body/already_marked: общая реализация трёх маркеров ─────────────


def test_marker_body_reads_new_unlabeled_marker():
    body = f"{sbg.UNLABELED_STALE_MARKER} #897]\nтекст"
    assert sbg.marker_body(body, sbg.UNLABELED_STALE_MARKER) == "#897"


def test_already_marked_true_for_unlabeled_episode():
    texts = ["шум", f"{sbg.UNLABELED_STALE_MARKER} #897]\nсигнал"]
    assert sbg.already_marked(texts, sbg.UNLABELED_STALE_MARKER, "#897") is True


def test_already_marked_false_for_different_marker_same_target():
    """Три маркера НЕ путают друг друга — один и тот же номер под другим
    маркером не гасит дедуп нового факта."""
    texts = [f"{sbg.MISSING_TARGET_MARKER} #897]\nсигнал"]
    assert sbg.already_marked(texts, sbg.UNLABELED_STALE_MARKER, "#897") is False


# ── unlabeled_stale_check: проводка на прод-формах, gh замокан ─────────────


def patch_task_deps_pool(monkeypatch, issues):
    monkeypatch.setattr(sbg.task_deps, "fetch_pool",
                         lambda repo, label="task", include_body=True: issues)


def test_unlabeled_stale_check_quiet_when_no_candidates(monkeypatch):
    patch_task_deps_pool(monkeypatch, [{"number": 1, "labels": [{"name": "task"}], "body": "нет маркера"}])
    assert sbg.unlabeled_stale_check(REPO) == [
        "💗 unlabeled: протухших объявлений не найдено (1 задач пула проверено)"]


def test_unlabeled_stale_check_skips_issue_with_blocked_label(monkeypatch):
    """Issue с меткой `blocked` — путь `stale_blocked_check`, не дублируется
    здесь, даже если тело несёт узнаваемый инлайн-маркер."""
    patch_task_deps_pool(monkeypatch, [{
        "number": 1016, "labels": [{"name": "task"}, {"name": "blocked"}], "body": ISSUE_1016_BODY,
    }])
    assert sbg.unlabeled_stale_check(REPO) == [
        "💗 unlabeled: протухших объявлений не найдено (1 задач пула проверено)"]


def test_unlabeled_stale_check_posts_comment_on_stale_inline_marker_1016(monkeypatch, offline_telegram):
    """Прод-форма #1016 (issue #1211): #897 закрыт — комментарий-факт
    оставляется прямо в issue, метки нет и снимать нечего."""
    calls = []

    def fake(*args):
        calls.append(args)
        if args[0] == "-X" and args[1] == "POST":
            return {"id": 1}
        url = args[0]
        if url == "repos/mytab0r/edge-harness/issues/897":
            return {"state": "closed", "closed_at": "2026-09-12T10:26:27Z"}
        if url == "repos/mytab0r/edge-harness/issues/1016/comments?per_page=100&page=1":
            return []
        raise AssertionError(f"неожиданный вызов gh: {args}")

    patch_gh(monkeypatch, fake)
    patch_task_deps_pool(monkeypatch, [
        {"number": 1016, "labels": [{"name": "task"}], "body": ISSUE_1016_BODY},
    ])
    lines = sbg.unlabeled_stale_check(REPO)
    assert len(lines) == 1
    assert lines[0].startswith("✅")
    assert "#1016" in lines[0] and "#897" in lines[0]

    posted = [c for c in calls if c[0] == "-X" and c[1] == "POST"]
    comment_calls = [c for c in posted if "issues/1016/comments" in c[2]]
    assert comment_calls
    body_arg = next(a for a in comment_calls[0] if a.startswith("body="))
    assert sbg.UNLABELED_STALE_MARKER in body_arg and "#897" in body_arg


def test_unlabeled_stale_check_silent_while_target_still_open(monkeypatch):
    """#717 в форме, где #713 ещё не закрыт — легитимно, молчим."""
    def fake(*args):
        url = args[0]
        if url == "repos/mytab0r/edge-harness/issues/713":
            return {"state": "open"}
        raise AssertionError(f"неожиданный вызов gh: {args}")

    patch_gh(monkeypatch, fake)
    patch_task_deps_pool(monkeypatch, [
        {"number": 717, "labels": [{"name": "task"}], "body": ISSUE_717_BODY},
    ])
    lines = sbg.unlabeled_stale_check(REPO)
    assert lines == ["💗 unlabeled: протухших объявлений не найдено (1 задач пула, "
                      "1 с объявленным блокером — все легитимны)"]


def test_unlabeled_stale_check_dedup_on_second_pass(monkeypatch):
    """Не спамь (issue #1211, требование 4): комментарий уже стоит в этом
    эпизоде — второй прогон молчит по каналу (💤), не постит второй раз."""
    def fake(*args):
        url = args[0]
        if url == "repos/mytab0r/edge-harness/issues/897":
            return {"state": "closed", "closed_at": "2026-09-12T10:26:27Z"}
        if url == "repos/mytab0r/edge-harness/issues/1016/comments?per_page=100&page=1":
            return [{"body": f"{sbg.UNLABELED_STALE_MARKER} #897]\nранее сказано"}]
        raise AssertionError(f"неожиданный вызов gh: {args}")

    patch_gh(monkeypatch, fake)
    patch_task_deps_pool(monkeypatch, [
        {"number": 1016, "labels": [{"name": "task"}], "body": ISSUE_1016_BODY},
    ])
    lines = sbg.unlabeled_stale_check(REPO)
    assert len(lines) == 1
    assert lines[0].startswith("💤")
    assert "#1016" in lines[0] and "#897" in lines[0]


def test_unlabeled_stale_check_posts_unrecognized_field_comment_768(monkeypatch):
    """Живой ложный позитив (issue #1215): #768 — поле не разбирается строго,
    третий исход (❓), не молчаливый пропуск и не ложное «протухло».

    Доводка ревью PR #1216 (пункт 2): ❓-исход получил канал доставки —
    идемпотентный комментарий С СВОИМ маркером эпизода
    (`UNRECOGNIZED_FIELD_MARKER`), называющий разборимую форму и газ (кто
    правит тело и когда шаг зеленеет). Лог прогона каналом доставки не
    считается: этот job уже однажды доказал, что его лог никто не читает
    (#268). ❓-строка при этом остаётся честно красной — тело всё ещё не по
    схеме.

    МУТАЦИЯ ИСПОЛНЕНА (issue #1194): в `unlabeled_stale_check` строка
    `if declared["unrecognized"]:` временно заменена на
    `if False and declared["unrecognized"]:` (2026-09-14, ручная правка перед
    коммитом) — ЭТОТ тест покраснел (неразборчивое поле перестаёт быть
    кандидатом, отчёт становится холостым 💗), мутация снята, тест снова
    зелёный."""
    calls = []

    def fake(*args):
        calls.append(args)
        if args[0] == "-X" and args[1] == "POST":
            return {"id": 1}
        url = args[0]
        if url == "repos/mytab0r/edge-harness/issues/768/comments?per_page=100&page=1":
            return []
        raise AssertionError(f"неожиданный вызов gh: {args}")

    patch_gh(monkeypatch, fake)
    patch_task_deps_pool(monkeypatch, [
        {"number": 768, "labels": [{"name": "task"}], "body": ISSUE_768_BODY},
    ])
    lines = sbg.unlabeled_stale_check(REPO)
    assert len(lines) == 1
    assert lines[0].startswith("❓")
    assert "#768" in lines[0]

    posted = [c for c in calls if c[0] == "-X" and c[1] == "POST"]
    comment_calls = [c for c in posted if "issues/768/comments" in c[2]]
    assert comment_calls, "❓-исход обязан доставляться комментарием в саму issue, не только логом"
    body_arg = next(a for a in comment_calls[0] if a.startswith("body="))
    assert sbg.UNRECOGNIZED_FIELD_MARKER in body_arg
    assert "ничем" in body_arg and "#N #M" in body_arg, (
        "комментарий обязан назвать обе разборимые формы ответа поля")


def test_unrecognized_field_marker_dedup_uses_field_text_as_episode(monkeypatch):
    """Дедуп на эпизод (issue #1211, требование 4 — тот же приём, что у
    `already_escalated`): телом эпизода служит сам неразборчивый текст поля —
    тот же текст даёт 💤 без повторного POST, правка текста автором открыла
    бы НОВЫЙ эпизод (факт «тело не разобрано» стал другим). Маркер-комментарий
    собирается тем же `unrecognized_field_value`, что и живой POST, — иначе
    тест сверял бы два разных пересказа, а не механизм."""
    marker_comment = (f"{sbg.UNRECOGNIZED_FIELD_MARKER} "
                      f"{sbg.unrecognized_field_value(ISSUE_768_BODY)}]\nранее сказано")

    def fake(*args):
        url = args[0]
        if url == "repos/mytab0r/edge-harness/issues/768/comments?per_page=100&page=1":
            return [{"body": marker_comment}]
        raise AssertionError(f"неожиданный вызов gh: {args}")

    patch_gh(monkeypatch, fake)
    patch_task_deps_pool(monkeypatch, [
        {"number": 768, "labels": [{"name": "task"}], "body": ISSUE_768_BODY},
    ])
    lines = sbg.unlabeled_stale_check(REPO)
    assert len(lines) == 1
    assert lines[0].startswith("💤"), "#768 и #971 — живые случаи этого исхода, повтор недопустим"
    assert "#768" in lines[0]


def test_unrecognized_field_value_normalizes_and_caps():
    """Тело эпизода: пробелы нормализованы, `]` вырезана (сломала бы разбор
    маркера-скобки `marker_body`), длинный текст урезан до потолка. Поле —
    ОДНА строка (`_FORM_FIELD_RE` читает `[^\n]*`), поэтому и форма теста
    однострочная, как живые #768/#971."""
    value = sbg.unrecognized_field_value("### Чем блокируется\n\nничего  (см. #361] и #286)\n")
    assert value == "ничего (см. #361) и #286)"
    assert "]" not in value
    long_value = sbg.unrecognized_field_value("### Чем блокируется\n\n" + "x" * 500)
    assert len(long_value) == sbg._UNRECOGNIZED_TARGET_MAX


def test_unlabeled_stale_check_posts_missing_target_comment(monkeypatch, offline_telegram):
    """Конструированная форма (см. докстринг `test_target_status_missing_
    on_real_gh_404_form` — живого случая на пуле 2026-09-14 не нашлось):
    объявленный номер не существует вовсе — третий, отдельный от «закрыт»,
    исход (AGENTS.md «Алерт не гадает»).

    МУТАЦИЯ ИСПОЛНЕНА (issue #1194, не описана по аналогии): в
    `unlabeled_stale_check` строка `if missing_targets:` временно заменена на
    `if False and missing_targets:` (2026-09-14, ручная правка перед
    коммитом), `python -m pytest scripts/orchestra/test_stale_blocked_guard.py
    -q -k missing` — ЭТОТ тест покраснел (`assert lines[0].startswith("✅")`
    упал: `missing`-цель проваливается в `all(s == "closed" ...)` → False →
    функция молча ничего не добавляет в `lines` → холостой 💗-отчёт вместо
    сообщения о несуществующем номере), мутация снята, тест снова зелёный
    (`63 passed`)."""
    calls = []

    def fake(*args):
        calls.append(args)
        if args[0] == "-X" and args[1] == "POST":
            return {"id": 1}
        url = args[0]
        if url == "repos/mytab0r/edge-harness/issues/999999":
            raise RuntimeError(
                "gh api repos/mytab0r/edge-harness/issues/999999: gh: Not Found (HTTP 404)")
        if url == "repos/mytab0r/edge-harness/issues/1/comments?per_page=100&page=1":
            return []
        raise AssertionError(f"неожиданный вызов gh: {args}")

    patch_gh(monkeypatch, fake)
    patch_task_deps_pool(monkeypatch, [
        {"number": 1, "labels": [{"name": "task"}], "body": "БЛОКИРУЕТСЯ: #999999\n"},
    ])
    lines = sbg.unlabeled_stale_check(REPO)
    assert len(lines) == 1
    assert lines[0].startswith("✅")
    assert "#999999" in lines[0]

    posted = [c for c in calls if c[0] == "-X" and c[1] == "POST"]
    comment_calls = [c for c in posted if "issues/1/comments" in c[2]]
    assert comment_calls
    body_arg = next(a for a in comment_calls[0] if a.startswith("body="))
    assert sbg.MISSING_TARGET_MARKER in body_arg and "#999999" in body_arg


def test_unlabeled_stale_check_reports_both_unrecognized_field_and_stale_inline(monkeypatch, offline_telegram):
    """Второе ревью PR #1216 (старшинство источников), полная проводка:
    битое поле (#768, прод-форма) и живая последняя строка
    «БЛОКИРУЕТСЯ: #897» (#897 закрыт) на одной issue дают ОБА факта —
    ❓-комментарий о поле (называющий найденный инлайн) и ✅-комментарий о
    протухшем объявлении, каждый со своим маркером эпизода. Прежнее
    поведение («битое поле глушит инлайн») теряло протухший #897 молча."""
    calls = []

    def fake(*args):
        calls.append(args)
        if args[0] == "-X" and args[1] == "POST":
            return {"id": 1}
        url = args[0]
        if url == "repos/mytab0r/edge-harness/issues/897":
            return {"state": "closed", "closed_at": "2026-09-12T10:26:27Z"}
        if url == "repos/mytab0r/edge-harness/issues/768/comments?per_page=100&page=1":
            return []
        raise AssertionError(f"неожиданный вызов gh: {args}")

    patch_gh(monkeypatch, fake)
    body = ISSUE_768_BODY + "БЛОКИРУЕТСЯ: #897\n"
    patch_task_deps_pool(monkeypatch, [
        {"number": 768, "labels": [{"name": "task"}], "body": body},
    ])
    lines = sbg.unlabeled_stale_check(REPO)
    assert len(lines) == 2, lines
    assert lines[0].startswith("❓") and "#768" in lines[0]
    assert lines[1].startswith("✅") and "#897" in lines[1]

    posted = [c for c in calls if c[0] == "-X" and c[1] == "POST"
              and "issues/768/comments" in c[2]]
    assert len(posted) == 2, "два факта — два комментария (дедуп у каждого свой)"
    bodies = [next(a for a in c if a.startswith("body=")) for c in posted]
    assert any(sbg.UNRECOGNIZED_FIELD_MARKER in b for b in bodies)
    assert any(sbg.UNLABELED_STALE_MARKER in b for b in bodies)
    unrecognized_body = next(b for b in bodies if sbg.UNRECOGNIZED_FIELD_MARKER in b)
    assert "БЛОКИРУЕТСЯ: #897" in unrecognized_body, (
        "❓-комментарий обязан назвать найденный инлайн-маркер — читатель видит, "
        "что объявление не потеряно")


def test_unlabeled_stale_check_escalates_when_comment_delivery_fails(monkeypatch, offline_telegram):
    """Второе ревью PR #1216 (провал доставки падает только в лог): отказ
    `post_issue_comment` на новом пути доставляется тем же каналом, что у
    помеченного пути — `pulse_guard.escalate` (повтор + Telegram), не
    только `::warning::` в лог прогона, который этот job уже однажды
    доказал нечитаемым (#268).

    МУТАЦИЯ: в `post_fact_comment` ветка `escalate(...)` удалена (только
    лог) — тест краснеет (эскалация не вызывается); мутация снята."""
    def fake(*args):
        if args[0] == "-X" and args[1] == "POST":
            raise RuntimeError("gh api: 502 Bad Response")
        url = args[0]
        if url == "repos/mytab0r/edge-harness/issues/897":
            return {"state": "closed", "closed_at": "2026-09-12T10:26:27Z"}
        if url == "repos/mytab0r/edge-harness/issues/1016/comments?per_page=100&page=1":
            return []
        raise AssertionError(f"неожиданный вызов gh: {args}")

    patch_gh(monkeypatch, fake)
    escalations: list[tuple] = []
    monkeypatch.setattr(sbg, "escalate",
                        lambda repo, number, text, **_: escalations.append((number, text))
                        or "Telegram: доставлен; след в #1016: оставлен")
    patch_task_deps_pool(monkeypatch, [
        {"number": 1016, "labels": [{"name": "task"}], "body": ISSUE_1016_BODY},
    ])
    lines = sbg.unlabeled_stale_check(REPO)
    assert len(lines) == 1
    assert lines[0].startswith("🚨") and "#897" in lines[0]
    assert "эскалировано тем же каналом" in lines[0]
    assert escalations and escalations[0][0] == 1016
    assert sbg.UNLABELED_STALE_MARKER in escalations[0][1], (
        "эскалация несёт тот же текст с маркером эпизода — доставленный "
        "повтором комментарий гасит дедуп следующего прогона")


def test_unlabeled_stale_check_reports_unknown_on_transport_failure(monkeypatch):
    """Сеть отказала на состоянии цели — третье состояние (❓), не молчаливое
    «протухших не найдено» (issue #1211, требование третьего состояния,
    scripts/lib/check_result.py)."""
    def fake(*args):
        url = args[0]
        if url == "repos/mytab0r/edge-harness/issues/897":
            raise RuntimeError("gh api repos/mytab0r/edge-harness/issues/897: connection reset")
        raise AssertionError(f"неожиданный вызов gh: {args}")

    patch_gh(monkeypatch, fake)
    patch_task_deps_pool(monkeypatch, [
        {"number": 1016, "labels": [{"name": "task"}], "body": ISSUE_1016_BODY},
    ])
    lines = sbg.unlabeled_stale_check(REPO)
    assert len(lines) == 1
    assert lines[0].startswith("❓")
    assert "#1016" in lines[0]


# ── Проводка: gh замокан, сеть не нужна ─────────────────────────────────────────


def patch_gh(monkeypatch, fake):
    monkeypatch.setattr(pg, "gh", fake)


def test_stale_blocked_check_zero_violations_is_quiet():
    """Холостой ход: нет issue с меткой blocked."""
    def fake(*args):
        if args[0].startswith("repos/mytab0r/edge-harness/issues?state=open&labels=blocked"):
            return []
        raise AssertionError(f"неожиданный вызов gh: {args}")

    original = pg.gh
    pg.gh = fake
    try:
        lines = sbg.stale_blocked_check(REPO)
    finally:
        pg.gh = original
    assert lines == ["💗 blocked: протухших меток не найдено (0 issue с меткой blocked проверено)"]


@pytest.fixture()
def offline_telegram(monkeypatch):
    """Без секретов Telegram честно «не доставлен» — сетью тест не ходит
    (тот же приём, что test_upstream_drift.offline_telegram)."""
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)


def test_stale_blocked_check_removes_label_and_leaves_trail_on_268_prod_form(monkeypatch, offline_telegram):
    """Полная проводка на прод-форме (#1157): listing → comments → issues/265
    (closed) → DELETE labels/blocked + POST комментарий-след В САМ #268 (не
    отдельная задача-статус, не Telegram — снятие успешно, эскалировать
    нечего)."""
    calls: list[tuple] = []

    def fake(*args):
        calls.append(args)
        if args[0] == "-X":
            return None
        url = args[0]
        if url.startswith("repos/mytab0r/edge-harness/issues?state=open&labels=blocked"):
            return [{
                "number": 268,
                "labels": [{"name": "task"}, {"name": "area:worker"}, {"name": "blocked"}],
                "body": ISSUE_268_BODY,
            }]
        if url == "repos/mytab0r/edge-harness/issues/268/comments?per_page=100&page=1":
            return [{"body": ISSUE_268_ESCALATION_COMMENT}]
        if url == "repos/mytab0r/edge-harness/issues/265":
            return {"state": "closed", "closed_at": "2026-09-05T13:33:09Z"}
        raise AssertionError(f"неожиданный вызов gh: {args}")

    patch_gh(monkeypatch, fake)
    lines = sbg.stale_blocked_check(REPO)
    assert len(lines) == 1
    assert lines[0].startswith("✅")
    assert "#268" in lines[0] and "#265" in lines[0]

    deletes = [c for c in calls if c[0] == "-X" and c[1] == "DELETE"]
    label_deletes = [c for c in deletes if "issues/268/labels/blocked" in c[2]]
    assert label_deletes, "метка blocked обязана быть снята с #268 запросом DELETE"

    posted = [c for c in calls if c[0] == "-X" and c[1] == "POST"]
    comment_calls = [c for c in posted if "issues/268/comments" in c[2]]
    assert comment_calls, "снятие обязано оставить след прямо в #268 (не в отдельной задаче-статус)"
    body_arg = next(a for a in comment_calls[0] if a.startswith("body="))
    assert "#265" in body_arg and "2026-09-05T13:33:09Z" in body_arg, (
        "след обязан называть закрытую задачу и когда она закрылась")


def test_stale_blocked_check_removes_label_on_215_prod_form_with_809_closed(monkeypatch, offline_telegram):
    """Живой случай #215/#258 (issue #938/#1157), маркер «Причина блокировки:
    #809», прод-форма комментария снята дословно выше. #809 закрыт
    2026-09-10T11:12:44Z (`gh issue view 809 --json closedAt`, снято при
    разборе этой задачи) — гвардия обязана снять метку сама, не только
    доложить о протухании."""
    calls: list[tuple] = []

    def fake(*args):
        calls.append(args)
        if args[0] == "-X":
            return None
        url = args[0]
        if url.startswith("repos/mytab0r/edge-harness/issues?state=open&labels=blocked"):
            return [{
                "number": 215,
                "labels": [{"name": "task"}, {"name": "white-spot"}, {"name": "blocked"},
                           {"name": "stale-unclaimed"}],
                "body": "тело #215 не участвует в этом тесте",
            }]
        if url == "repos/mytab0r/edge-harness/issues/215/comments?per_page=100&page=1":
            return [{"body": ISSUE_215_ESCALATION_COMMENT}]
        if url == "repos/mytab0r/edge-harness/issues/809":
            return {"state": "closed", "closed_at": "2026-09-10T11:12:44Z"}
        raise AssertionError(f"неожиданный вызов gh: {args}")

    patch_gh(monkeypatch, fake)
    lines = sbg.stale_blocked_check(REPO)
    assert len(lines) == 1
    assert lines[0].startswith("✅")
    assert "#215" in lines[0] and "#809" in lines[0]

    deletes = [c for c in calls if c[0] == "-X" and c[1] == "DELETE"]
    assert any("issues/215/labels/blocked" in c[2] for c in deletes), (
        "метка blocked обязана быть снята с #215")
    posted = [c for c in calls if c[0] == "-X" and c[1] == "POST"]
    comment_calls = [c for c in posted if "issues/215/comments" in c[2]]
    assert comment_calls
    body_arg = next(a for a in comment_calls[0] if a.startswith("body="))
    assert "#809" in body_arg and "2026-09-10T11:12:44Z" in body_arg


def test_stale_blocked_check_escalates_when_label_removal_fails(monkeypatch, offline_telegram):
    """Газ у газа наоборот: снятие само провалилось (сеть/права) — метка
    остаётся, но провал не должен тонуть молча. Эскалация тем же каналом,
    что раньше несла само протухание (комментарий в #268 + Telegram)."""
    calls: list[tuple] = []

    def fake(*args):
        calls.append(args)
        if args[0] == "-X" and args[1] == "DELETE":
            raise RuntimeError("gh api: 403 Forbidden")
        if args[0] == "-X":
            return None
        url = args[0]
        if url.startswith("repos/mytab0r/edge-harness/issues?state=open&labels=blocked"):
            return [{
                "number": 268,
                "labels": [{"name": "task"}, {"name": "area:worker"}, {"name": "blocked"}],
                "body": ISSUE_268_BODY,
            }]
        if url == "repos/mytab0r/edge-harness/issues/268/comments?per_page=100&page=1":
            return [{"body": ISSUE_268_ESCALATION_COMMENT}]
        if url == "repos/mytab0r/edge-harness/issues/265":
            return {"state": "closed", "closed_at": "2026-09-05T13:33:09Z"}
        raise AssertionError(f"неожиданный вызов gh: {args}")

    patch_gh(monkeypatch, fake)
    lines = sbg.stale_blocked_check(REPO)
    assert len(lines) == 1
    assert lines[0].startswith("🚨")
    assert "#268" in lines[0] and "#265" in lines[0] and "НЕ снята" in lines[0]

    posted = [c for c in calls if c[0] == "-X" and c[1] == "POST"]
    comment_calls = [c for c in posted if "issues/268/comments" in c[2]]
    assert comment_calls, "провал снятия обязан оставить след эскалации в #268"
    body_arg = next(a for a in comment_calls[0] if a.startswith("body="))
    assert "[протухшая блокировка: #265]" in body_arg


def test_stale_blocked_check_silent_channel_when_removal_failure_already_escalated(monkeypatch, offline_telegram):
    """Тот же провал снятия повторяется каждый пульс (403 не лечится само) —
    второй прогон не шлёт повтор в канал (иначе 15-минутный крон заспамит),
    но нарушение остаётся в отчёте (не 💗) — CI-шаг не должен выглядеть
    холостым."""
    prior_escalation = "🚨 [протухшая блокировка: #265]\nуже сигналили раньше"

    def fake(*args):
        if args[0] == "-X" and args[1] == "DELETE":
            raise RuntimeError("gh api: 403 Forbidden")
        if args[0] == "-X":
            raise AssertionError(f"мутирующий вызов не ожидался — эпизод уже сигналился: {args}")
        url = args[0]
        if url.startswith("repos/mytab0r/edge-harness/issues?state=open&labels=blocked"):
            return [{
                "number": 268,
                "labels": [{"name": "task"}, {"name": "area:worker"}, {"name": "blocked"}],
                "body": ISSUE_268_BODY,
            }]
        if url == "repos/mytab0r/edge-harness/issues/268/comments?per_page=100&page=1":
            return [{"body": ISSUE_268_ESCALATION_COMMENT}, {"body": prior_escalation}]
        if url == "repos/mytab0r/edge-harness/issues/265":
            return {"state": "closed", "closed_at": "2026-09-05T13:33:09Z"}
        raise AssertionError(f"неожиданный вызов gh: {args}")

    patch_gh(monkeypatch, fake)
    lines = sbg.stale_blocked_check(REPO)
    assert len(lines) == 1
    assert lines[0].startswith("🔇")
    assert "#268" in lines[0] and "#265" in lines[0]


def test_stale_blocked_check_quiet_once_label_already_gone():
    """Идемпотентность (#1157): после успешного снятия следующий прогон уже
    не видит issue в списке `labels=blocked` вовсе (метка снята) — холостой
    ход, без бесконечного цикла снятие→находка→снятие."""
    def fake(*args):
        if args[0].startswith("repos/mytab0r/edge-harness/issues?state=open&labels=blocked"):
            return []
        raise AssertionError(f"неожиданный вызов gh: {args}")

    original = pg.gh
    pg.gh = fake
    try:
        lines = sbg.stale_blocked_check(REPO)
    finally:
        pg.gh = original
    assert lines == ["💗 blocked: протухших меток не найдено (0 issue с меткой blocked проверено)"]


def test_stale_blocked_check_silent_on_216_live_shaped_responses(monkeypatch):
    """Та же проводка, но на форме #216: #163/#164 закрыты — отчёт обязан
    остаться холостым (живой контрпример ложного срабатывания)."""
    def fake(*args):
        url = args[0]
        if url.startswith("repos/mytab0r/edge-harness/issues?state=open&labels=blocked"):
            return [{
                "number": 216,
                "labels": [{"name": "task"}, {"name": "blocked"}],
                "body": ISSUE_216_BODY,
            }]
        if url == "repos/mytab0r/edge-harness/issues/216/comments?per_page=100&page=1":
            return []
        raise AssertionError(f"неожиданный вызов gh: {args}")

    patch_gh(monkeypatch, fake)
    lines = sbg.stale_blocked_check(REPO)
    assert lines == ["💗 blocked: протухших меток не найдено (1 issue с меткой blocked проверено)"]


# ── Доводка ревью PR #1216 (пункт 1): помеченная issue с битой целью — ❓, не крах ──

def test_stale_blocked_check_missing_target_reports_question_mark_without_removal(monkeypatch):
    """Живой дефект до доводки: маркер «Блокирована: #N» с несуществующим N
    ронял ВСЮ stale_blocked_check исключением (issue_state без обработки 404),
    а main() исполняет обходы последовательно — крах старого пути хоронил и
    новый. Теперь: ❓-строка (третье состояние, issue #1211), метка НЕ тронута
    (факт протухания не подтверждён — «Алерт не гадает»), никакого исключения.

    МУТАЦИЯ ИСПОЛНЕНА (issue #1194): в `stale_blocked_check` ветка
    `if missing:` (обе строки ❓-блока) временно удалена (2026-09-14, ручная
    правка перед коммитом) — ЭТОТ тест покраснел (`lines[0].startswith("❓")`
    упал: missing-цель молча пропускалась, отчёт становился холостым 💗),
    мутация снята, тест снова зелёный."""
    calls: list[tuple] = []

    def fake(*args):
        calls.append(args)
        url = args[0]
        if url.startswith("repos/mytab0r/edge-harness/issues?state=open&labels=blocked"):
            return [{
                "number": 215,
                "labels": [{"name": "task"}, {"name": "blocked"}],
                "body": "тело #215 не участвует в этом тесте",
            }]
        if url == "repos/mytab0r/edge-harness/issues/215/comments?per_page=100&page=1":
            return [{"body": "Причина блокировки: #999999."}]
        if url == "repos/mytab0r/edge-harness/issues/999999":
            raise RuntimeError(
                "gh api repos/mytab0r/edge-harness/issues/999999: gh: Not Found (HTTP 404)")
        raise AssertionError(f"неожиданный вызов gh: {args}")

    patch_gh(monkeypatch, fake)
    lines = sbg.stale_blocked_check(REPO)
    assert len(lines) == 1
    assert lines[0].startswith("❓")
    assert "#215" in lines[0] and "#999999" in lines[0]
    assert not any(c[0] == "-X" and c[1] == "DELETE" for c in calls), (
        "метка не снимается, пока названный номер не подтвердился существующим")


def test_stale_blocked_check_unknown_target_reports_question_mark(monkeypatch):
    """Разовый сбой сети на состоянии цели — ❓ (причина не установлена),
    не исключение на всю проводку и не молчаливое «протухших нет»."""
    def fake(*args):
        url = args[0]
        if url.startswith("repos/mytab0r/edge-harness/issues?state=open&labels=blocked"):
            return [{
                "number": 215,
                "labels": [{"name": "task"}, {"name": "blocked"}],
                "body": "тело #215 не участвует в этом тесте",
            }]
        if url == "repos/mytab0r/edge-harness/issues/215/comments?per_page=100&page=1":
            return [{"body": ISSUE_215_ESCALATION_COMMENT}]
        if url == "repos/mytab0r/edge-harness/issues/809":
            raise RuntimeError("gh api repos/mytab0r/edge-harness/issues/809: connection reset")
        raise AssertionError(f"неожиданный вызов gh: {args}")

    patch_gh(monkeypatch, fake)
    lines = sbg.stale_blocked_check(REPO)
    assert len(lines) == 1
    assert lines[0].startswith("❓")
    assert "#215" in lines[0] and "#809" in lines[0]


def test_main_isolates_sweeps_when_one_raises(monkeypatch, capsys):
    """Изоляция путей (доводка ревью PR #1216, пункт 1): падение целого
    labeled-обхода не хоронит отчёт unlabeled-обхода — в отчёте ОБЕ строки
    (🚨 про упавший обход и 💗 от досчитавшегося), шаг красный."""
    def broken(repo):
        raise RuntimeError("gh api repos/.../issues?labels=blocked: connection reset")

    monkeypatch.setattr(sbg, "stale_blocked_check", broken)
    monkeypatch.setattr(sbg, "unlabeled_stale_check", lambda repo: ["💗 unlabeled: холостой ход"])
    assert sbg.main() == 1
    out = capsys.readouterr().out
    assert "💗 unlabeled: холостой ход" in out, "второй обход обязан досчитаться"
    assert "🚨" in out and "blocked (метки)" in out


# ── Доводка ревью PR #1216 (пункт 3): структурное поле как причина метки ──

def test_find_stale_blocked_flags_structural_source_without_inline_marker():
    """Пересечение «метка blocked + поле «Чем блокируется» на закрытый номер»
    (доводка ревью, пункт 3): раньше не видел ни один путь — unlabeled
    пропускал помеченные, labeled читал только инлайн-маркер. Прод-форма
    поля — та же, что у непомеченной #741 (#740 закрыт 2026-09-08)."""
    issue = {
        "number": 741,
        "labels": [{"name": "task"}, {"name": "blocked"}],
        "body": ISSUE_741_BODY,
        "comments_text": [],
    }
    violations = sbg.find_stale_blocked([issue], closed_numbers={740},
                                        structural_by_number={741: [740]})
    assert violations == [{"number": 741, "stale_refs": [740]}]


def test_find_stale_blocked_silent_while_structural_target_open():
    """Зеркало: поле называет ещё открытый номер — блокировка законна, тишина."""
    issue = {
        "number": 741,
        "labels": [{"name": "task"}, {"name": "blocked"}],
        "body": ISSUE_741_BODY,
        "comments_text": [],
    }
    violations = sbg.find_stale_blocked([issue], closed_numbers=set(),
                                        structural_by_number={741: [740]})
    assert violations == []


def test_find_stale_blocked_inline_marker_beats_structural_field():
    """Старшинство (доводка ревью, пункт 3): последний инлайн-маркер — более
    свежее высказывание о причине метки, чем поле формы при заведении. Живой
    инлайн-маркер называет открытый #300 → метка законна, протухшее поле
    решения о снятии НЕ выносит (консервативно: безальтернативно протухшая
    причина или ничего)."""
    issue = {
        "number": 300,
        "labels": [{"name": "task"}, {"name": "blocked"}],
        "body": ISSUE_741_BODY + "\nБлокирована: #301\n",
        "comments_text": [],
    }
    violations = sbg.find_stale_blocked([issue], closed_numbers={740},
                                        structural_by_number={300: [740]})
    assert violations == []


def test_stale_blocked_check_removes_label_on_structural_only_cause(monkeypatch, offline_telegram):
    """Полная проводка структурной причины метки (доводка ревью, пункт 3):
    поле «Чем блокируется» → #740 закрыт, инлайн-маркера нет нигде — метка
    снимается автоматически, след называет закрытый номер и когда.

    МУТАЦИЯ ИСПОЛНЕНА (issue #1194): в `stale_blocked_check` строка
    `if kind == "declared" and structural:` временно заменена на
    `if False and kind == "declared" and structural:` (2026-09-14, ручная
    правка перед коммитом) — ЭТОТ тест покраснел (`structural_by_number`
    пуст, отчёт становится холостым 💗: пересечение «метка + протухшее поле»
    снова никем не проверяется), мутация снята, тест снова зелёный."""
    calls: list[tuple] = []

    def fake(*args):
        calls.append(args)
        if args[0] == "-X":
            return None
        url = args[0]
        if url.startswith("repos/mytab0r/edge-harness/issues?state=open&labels=blocked"):
            return [{
                "number": 741,
                "labels": [{"name": "task"}, {"name": "blocked"}],
                "body": ISSUE_741_BODY,
            }]
        if url == "repos/mytab0r/edge-harness/issues/741/comments?per_page=100&page=1":
            return []
        if url == "repos/mytab0r/edge-harness/issues/740":
            return {"state": "closed", "closed_at": "2026-09-08T18:20:11Z"}
        raise AssertionError(f"неожиданный вызов gh: {args}")

    patch_gh(monkeypatch, fake)
    lines = sbg.stale_blocked_check(REPO)
    assert len(lines) == 1
    assert lines[0].startswith("✅")
    assert "#741" in lines[0] and "#740" in lines[0]

    deletes = [c for c in calls if c[0] == "-X" and c[1] == "DELETE"]
    assert any("issues/741/labels/blocked" in c[2] for c in deletes)
    posted = [c for c in calls if c[0] == "-X" and c[1] == "POST"]
    comment_calls = [c for c in posted if "issues/741/comments" in c[2]]
    assert comment_calls
    body_arg = next(a for a in comment_calls[0] if a.startswith("body="))
    assert "#740" in body_arg and "2026-09-08T18:20:11Z" in body_arg


def _quiet_unlabeled(monkeypatch):
    """main() с #1211 зовёт ОБА прогона — тесты exit-кода ниже проверяют
    только исход stale_blocked_check, поэтому unlabeled_stale_check
    заглушается холостым (немоканный прогон уходит в живую сеть — 69 с
    живых gh-вызовов, найдено этой же правкой на первом запуске тестов)."""
    monkeypatch.setattr(sbg, "unlabeled_stale_check", lambda repo: ["💗 unlabeled: холостой ход"])


def test_main_exit_code_reflects_violations(monkeypatch):
    _quiet_unlabeled(monkeypatch)
    monkeypatch.setattr(sbg, "stale_blocked_check", lambda repo: ["🚨 нарушение"])
    assert sbg.main() == 1

    monkeypatch.setattr(sbg, "stale_blocked_check", lambda repo: ["💗 всё чисто"])
    assert sbg.main() == 0


def test_main_exit_code_treats_automatic_removal_as_green(monkeypatch):
    """#1157: находка, УЖЕ ПОЧИНЕННАЯ этим же прогоном (метка снята
    автоматически) — не считается нарушением, шаг остаётся зелёным."""
    _quiet_unlabeled(monkeypatch)
    monkeypatch.setattr(sbg, "stale_blocked_check", lambda repo: ["✅ метка `blocked` снята автоматически"])
    assert sbg.main() == 0


def test_main_exit_code_treats_silent_channel_as_violation_too(monkeypatch):
    """Находка ревью #333/#336 (сохраняется для отказа снятия): «уже
    эскалировано, повтор не шлём» — это всё ещё нарушение (метка не снята),
    а не холостой ход. Молчит только канал эскалации, CI-шаг остаётся
    красным."""
    _quiet_unlabeled(monkeypatch)
    monkeypatch.setattr(sbg, "stale_blocked_check", lambda repo: ["🔇 нарушение, но эпизод уже сигналился"])
    assert sbg.main() == 1


def test_main_exit_code_treats_unlabeled_dedup_as_green(monkeypatch):
    """Зеркало предыдущего теста для НОВОГО пути (issue #1211): дедуп
    «уже сообщено в этом эпизоде» (💤) для issue БЕЗ метки — не нарушение
    (нечего чинить, комментарий уже стоит), в отличие от 🔇 labeled-пути
    (там 🔇 означает «метка НЕ снята», genuine failure — разные факты,
    разные исходы, AGENTS.md «Алерт не гадает»)."""
    monkeypatch.setattr(sbg, "stale_blocked_check", lambda repo: ["💗 всё чисто"])
    monkeypatch.setattr(sbg, "unlabeled_stale_check", lambda repo: ["💤 #1: уже сообщено в этом эпизоде"])
    assert sbg.main() == 0


def test_main_exit_code_treats_unlabeled_unknown_as_violation(monkeypatch):
    """❓ (не удалось проверить/разобрать, issue #1211 третье состояние) —
    красный шаг, не молчаливое здоровье."""
    monkeypatch.setattr(sbg, "stale_blocked_check", lambda repo: ["💗 всё чисто"])
    monkeypatch.setattr(sbg, "unlabeled_stale_check", lambda repo: ["❓ #1: не подтверждено"])
    assert sbg.main() == 1
