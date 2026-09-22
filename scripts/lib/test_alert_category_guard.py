#!/usr/bin/env python3
"""Тесты гвардии «сигнал без категории» (#1461).

Поведенческие: каждый сценарий — настоящий файл на диске (.py, .sh или .ts),
который гвардия разбирает своим обычным путём (AGENTS.md, #891/#893), а не
сверка с копией правила в тесте.
"""

# --- console_utf8 bootstrap (класс: печать кириллицы валит encoding на Windows, issue #723) ---
import importlib.util
from pathlib import Path
_console_utf8_spec = importlib.util.spec_from_file_location(
    "console_utf8", Path(__file__).resolve().parent / "console_utf8.py")
_console_utf8_spec.loader.exec_module(importlib.util.module_from_spec(_console_utf8_spec))
# --- конец console_utf8 bootstrap ---

import sys

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "alert_category_guard", Path(__file__).resolve().parent / "alert_category_guard.py")
guard = importlib.util.module_from_spec(_SPEC)
sys.modules["alert_category_guard"] = guard
_SPEC.loader.exec_module(guard)


def _repo(tmp_path: Path, name: str, source: str) -> Path:
    root = tmp_path / "repo"
    (root / "scripts" / "orchestra").mkdir(parents=True, exist_ok=True)
    (root / "scripts" / "orchestra" / name).write_text(source, encoding="utf-8")
    return root


def test_call_without_category_is_found(tmp_path):
    root = _repo(tmp_path, "sender.py", "send_telegram('текст')\n")

    problems = guard.check(root)

    assert len(problems) == 1
    assert "sender.py:1" in problems[0]
    assert "alert_category" in problems[0], "сообщение обязано назвать, где взять категорию"


def test_call_with_category_passes(tmp_path):
    root = _repo(tmp_path, "sender.py", "send_telegram('текст', category='breakage')\n")

    assert guard.check(root) == []


def test_multiline_call_with_category_below_is_not_a_false_positive(tmp_path):
    """Урок #1438, оплаченный в этой же сессии: текстовый поиск `send_telegram(`
    видит только первую строку и назвал бы этот вызов нарушением. AST видит
    вызов целиком — именно поэтому разбор здесь AST, а не регулярка."""
    root = _repo(tmp_path, "sender.py", """
send_telegram(
    build_text(),
    as_html=True,
    category='pipeline',
)
""")

    assert guard.check(root) == []


def test_multiline_call_without_category_is_still_found(tmp_path):
    """Обратная сторона той же монеты: перенос строк не должен ПРЯТАТЬ
    нарушение. Гвардия, ловящая только однострочные вызовы, ложно-зелёная."""
    root = _repo(tmp_path, "sender.py", """
send_telegram(
    build_text(),
    as_html=True,
)
""")

    problems = guard.check(root)

    assert len(problems) == 1
    assert "sender.py:2" in problems[0]


def test_attribute_call_is_seen_too(tmp_path):
    """Отправитель зовут и как `pulse_guard.send_telegram(...)` — через
    атрибут модуля. Проверка только по голому имени пропустила бы весь
    scheduler.py, где он вызывается именно так."""
    root = _repo(tmp_path, "caller.py", "pulse_guard.send_telegram('текст')\n")

    assert len(guard.check(root)) == 1


def test_the_sender_definition_itself_is_not_an_offender(tmp_path):
    """`def send_telegram(...)` — не вызов. Считать его нарушением значило бы
    штрафовать файл, который эту возможность и предоставляет."""
    root = _repo(tmp_path, "sender.py",
                 "def send_telegram(text, *, category):\n    return True\n")

    assert guard.check(root) == []


def test_tests_are_not_scanned(tmp_path):
    """Тесты зовут отправитель со стендами, владельцу они ничего не шлют.
    Требовать категорию там — тормоз без причины."""
    root = tmp_path / "repo"
    (root / "scripts" / "orchestra").mkdir(parents=True, exist_ok=True)
    (root / "scripts" / "orchestra" / "test_x.py").write_text(
        "send_telegram('текст')\n", encoding="utf-8")

    assert guard.check(root) == []


def test_live_repository_is_clean():
    """На живом дереве гвардия обязана быть зелёной: все девять замеренных
    точек отправки несут категорию."""
    assert guard.check() == []


# ── значение аргумента, а не только его наличие (#1461, живая ошибка автора) ──


def test_unresolvable_attribute_chain_is_found(tmp_path):
    """Живая ошибка автора в этом же PR: `category=` на месте, аргумент есть,
    гвардия молчала — а имя `pulse_guard` в тот модуль не импортировано, и
    упало NameError'ом в рантайме. Проверять наличие аргумента мало."""
    root = _repo(tmp_path, "caller.py",
                 "send_telegram('текст', category=pulse_guard.alert_category.BREAKAGE)\n")

    problems = guard.check(root)

    assert len(problems) == 1
    assert "не на реестр" in problems[0]


def test_registry_constant_passes(tmp_path):
    root = _repo(tmp_path, "caller.py",
                 "send_telegram('текст', category=alert_category.BREAKAGE)\n")

    assert guard.check(root) == []


def test_known_string_literal_passes(tmp_path):
    """Строка допустима — но только объявленная: реестр читается по-настоящему,
    а не сверяется с «похоже на категорию»."""
    root = _repo(tmp_path, "caller.py", "send_telegram('текст', category='pipeline')\n")

    assert guard.check(root) == []


def test_unknown_string_literal_is_found(tmp_path):
    root = _repo(tmp_path, "caller.py", "send_telegram('текст', category='прочее')\n")

    problems = guard.check(root)

    assert len(problems) == 1
    assert "не объявлена" in problems[0]


def test_runtime_computed_value_is_found(tmp_path):
    """Переменная или вызов статически не разрешаются. Пропустить их молча
    значило бы оставить ровно ту дверь, через которую ошибка автора и вошла."""
    root = _repo(tmp_path, "caller.py", "send_telegram('текст', category=pick())\n")

    assert len(guard.check(root)) == 1


def test_typo_in_registry_constant_is_found(tmp_path):
    """Находка ai-ревью PR #1462: `alert_category.PIEPLEINE` проходит проверку
    «ссылается на реестр», гвардия молчит — а в рантайме редкой ветки падает
    AttributeError'ом. Базы недостаточно: имя сверяется с реестром."""
    root = _repo(tmp_path, "caller.py",
                 "send_telegram('текст', category=alert_category.PIEPLEINE)\n")

    problems = guard.check(root)

    assert len(problems) == 1
    assert "PIEPLEINE не объявлена" in problems[0]
    assert "PIPELINE" in problems[0], "подсказка обязана называть годные имена дословно"


def test_non_category_registry_attribute_is_found(tmp_path):
    """Атрибут реестра, не являющийся категорией (`CATEGORY_ORDER`), — тоже
    нарушение: в `category=` нужно значение реестра, а не любой его атрибут."""
    root = _repo(tmp_path, "caller.py",
                 "send_telegram('текст', category=alert_category.CATEGORY_ORDER)\n")

    assert len(guard.check(root)) == 1


# ── второй отправитель: bash telegram_report (находка ai-ревью PR #1462) ────────


def _repo_sh(tmp_path: Path, name: str, source: str) -> Path:
    root = tmp_path / "repo"
    (root / "scripts" / "worker").mkdir(parents=True, exist_ok=True)
    (root / "scripts" / "worker" / name).write_text(source, encoding="utf-8")
    return root


def test_bash_call_without_category_is_found(tmp_path):
    """Живой дефект этого PR: три из четырёх вызовов task.sh остались со старой
    сигнатурой — текст вместо категории. alert_prefix валится, `|| true`
    глотает отказ, job зелёный, а владелец не узнаёт ни об эскалации, ни о
    провале. Python-часть гвардии эти файлы не читала."""
    root = _repo_sh(tmp_path, "task.sh",
                    'telegram_report "worker: задача #7 — ПРОВАЛ" || true\n')

    problems = guard.check(root)

    assert len(problems) == 1
    assert "task.sh:1" in problems[0]
    assert "не разрешается статически" in problems[0]


def test_bash_call_with_known_category_passes(tmp_path):
    root = _repo_sh(tmp_path, "task.sh",
                    'telegram_report decision "worker: эскалация владельцу" || true\n')

    assert guard.check(root) == []


def test_bash_call_with_unknown_word_is_found(tmp_path):
    root = _repo_sh(tmp_path, "task.sh", 'telegram_report urgent "текст"\n')

    problems = guard.check(root)

    assert len(problems) == 1
    assert "'urgent' не объявлена" in problems[0]


def test_bash_call_with_variable_is_found(tmp_path):
    """Переменная статически не разрешается так же, как текст: пропускать её
    значило бы оставить дверь, через которую категория снова стала бы
    необязательной."""
    root = _repo_sh(tmp_path, "task.sh", 'telegram_report "$category" "текст"\n')

    assert len(guard.check(root)) == 1


def test_bash_continuation_call_is_seen(tmp_path):
    r"""Вызов, перенесённый `\`, не должен прятать нарушение — тот же урок, что
    у многострочного вызова на Python (#1438)."""
    root = _repo_sh(tmp_path, "task.sh", 'telegram_report \\\n  "текст без категории"\n')

    assert len(guard.check(root)) == 1


def test_bash_definition_comments_and_warnings_are_not_offenders(tmp_path):
    """Определение отправителя, его warning о чужой ошибке и комментарий с
    именем функции — не вызовы. Срезать по первому `#` нельзя: сами тексты
    несут «задача #123»."""
    root = _repo_sh(tmp_path, "task.sh",
                    'telegram_report() { :; }\n'
                    'echo "::warning::telegram_report вызван с неизвестной категорией"\n'
                    '# второй отправитель — telegram_report в task.sh\n'
                    'telegram_report pipeline "задача #5 — отчёт" || true\n')

    assert guard.check(root) == []


def test_bash_test_scripts_are_not_scanned(tmp_path):
    """Смоки зовут отправитель через заглушку (`telegram_report() { … }`) —
    владельцу они ничего не отправляют."""
    root = tmp_path / "repo"
    (root / "scripts" / "worker" / "test").mkdir(parents=True)
    (root / "scripts" / "worker" / "test" / "x.smoke.sh").write_text(
        'telegram_report "текст"\n', encoding="utf-8")

    assert guard.check(root) == []


# ── третий отправитель: TS #telegramApi("sendMessage") (находка ai-ревью) ───────


def _repo_ts(tmp_path: Path, name: str, source: str) -> Path:
    root = tmp_path / "repo"
    (root / "cf-worker" / "src").mkdir(parents=True, exist_ok=True)
    (root / "cf-worker" / "src" / name).write_text(source, encoding="utf-8")
    return root


def test_ts_send_message_without_decorate_alert_is_found(tmp_path):
    """Второй живой дефект этого PR: `#tickPulseAlert` шлёт инцидент и
    recovery голым текстом рядом с оформленным `#tickStorageReadyAlert` —
    а существующая спека проверяет только `toContain` и этого не видит."""
    root = _repo_ts(tmp_path, "harness.ts",
                    'class H { go() { void this.#telegramApi("sendMessage", '
                    '{ chat_id: 1, text: pulseAlertText(last) }); } }\n')

    problems = guard.check(root)

    assert len(problems) == 1
    assert "harness.ts:1" in problems[0]
    assert "без decorateAlert" in problems[0]


def test_ts_send_message_with_decorate_alert_passes(tmp_path):
    root = _repo_ts(tmp_path, "harness.ts",
                    'class H { go() { void this.#telegramApi("sendMessage", '
                    '{ text: decorateAlert("breakage", "инцидент") }); } }\n')

    assert guard.check(root) == []


def test_ts_decorate_alert_with_unknown_category_is_found(tmp_path):
    root = _repo_ts(tmp_path, "harness.ts",
                    'class H { go() { void this.#telegramApi("sendMessage", '
                    '{ text: decorateAlert("urgent", "x") }); } }\n')

    assert len(guard.check(root)) == 1


def test_ts_decorate_alert_with_variable_is_found(tmp_path):
    root = _repo_ts(tmp_path, "harness.ts",
                    'class H { go(cat: string) { void this.#telegramApi("sendMessage", '
                    '{ text: decorateAlert(cat, "x") }); } }\n')

    assert len(guard.check(root)) == 1


def test_ts_send_message_without_text_is_found(tmp_path):
    root = _repo_ts(tmp_path, "harness.ts",
                    'class H { go(p: unknown) { void this.#telegramApi("sendMessage", p); } }\n')

    assert len(guard.check(root)) == 1


def test_ts_non_message_methods_are_not_new_signals(tmp_path):
    """`answerCallbackQuery` — тост-ответ на нажатие кнопки самим владельцем,
    `editMessageText` — правка УЖЕ категоризованного сообщения решения:
    нового сигнала в общий поток они не создают (честная граница, названная
    в дельта-спеке). Новый sendMessage мимо decorateAlert — красится."""
    root = _repo_ts(tmp_path, "harness.ts",
                    'class H {\n'
                    '  a() { void this.#telegramApi("answerCallbackQuery", { text: "Принято" }); }\n'
                    '  b() { void this.#telegramApi("editMessageText", { text: `${t}\\n✅ ок` }); }\n'
                    '}\n')

    assert guard.check(root) == []


def test_ts_spec_files_are_not_scanned(tmp_path):
    root = tmp_path / "repo"
    (root / "cf-worker" / "src").mkdir(parents=True)
    (root / "cf-worker" / "src" / "x.spec.ts").write_text(
        'it("x", () => { this.#telegramApi("sendMessage", { text: "голый" }); });\n',
        encoding="utf-8")

    assert guard.check(root) == []


def test_ts_paren_inside_string_does_not_break_body_extraction(tmp_path):
    """Баланс скобок обязан пропускать строковые литералы целиком: скобка в
    тексте сообщения (или в `?.trim()`) не обрывает payload на середине —
    иначе гвардия молча пропускала бы всё, что идёт после такой строки."""
    root = _repo_ts(tmp_path, "harness.ts",
                    'class H { go() { void this.#telegramApi("sendMessage", '
                    '{ text: decorateAlert("infra", "деплой (ок) (снова)") }); } }\n')

    assert guard.check(root) == []
