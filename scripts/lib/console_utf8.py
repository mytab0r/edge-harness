#!/usr/bin/env python3
"""Единая точка правды для кодировки stdout/stderr (issue #723): любой
скрипт этого репозитория печатает русский текст, а точка входа — Windows.

Дефолт CPython на Windows опасен ровно там, где вывод НЕ подключён к живой
консоли (redirected/piped — `$(...)` в bash, `subprocess.run(capture_output=
True)`, любой инструмент-агент, перехватывающий stdout, чтобы показать его
дальше): в этом случае Python использует legacy `io.TextIOWrapper` поверх
ANSI-кодовой страницы локали (`cp1251` на ru-RU Windows) с обработчиком
ошибок `strict` для stdout — печать эмодзи (`🔒`, `⚠️`, …) падает
`UnicodeEncodeError` сразу. Живая консоль (`_WindowsConsoleIO`, PEP 528)
обычно не страдает — но настраивать поток нужно ДО первого print, а не
гадать, какой из двух режимов достанется на этот раз.

Воспроизведено (issue #723): `PYTHONIOENCODING=cp1251 python3 -c
"print('🔒 Аренда задачи')"` — `UnicodeEncodeError: 'charmap' codec can't
encode character '\\U0001f512'`; `PYTHONIOENCODING=cp866 python3 -c
"print('задача закрыта — аренда не выдана')"` — падает на обычном
em-dash «—», которого нет и в кириллической OEM-странице консоли.

Фикс — один вызов `ensure_utf8_stdio()` на верхнем уровне модуля (не внутри
`if __name__ == "__main__":`, чтобы сработало и при обычном запуске как
скрипта, и при импорте функций другим скриптом через `importlib.util`,
как это уже принято в этом репозитории). `errors="backslashreplace"` — тот
же обработчик, что CPython уже применяет к stderr по умолчанию: экранирует
непредставимый символ вместо падения, не проглатывает его молча (сообщение
остаётся читаемым, просто с `\\uXXXX` на месте символа не той кодовой
страницы, а не крахом всего скрипта).

Сестринский класс (декодирование, не кодирование) — `subprocess.run(...,
text=True)` без явного `encoding="utf-8"` в местах, вызывающих `gh api`:
дефолт тот же самый (ANSI-кодовая страница локали), и падает симметрично,
`UnicodeDecodeError`, на UTF-8 JSON-ответе GitHub с не-ASCII байтами
(заголовок/тело issue на русском). Он чинится не этим модулем, а явным
`encoding="utf-8"` в каждом вызове `subprocess.run` — самостоятельная
правка per-файл (issue #723, живой трейс: `task-branch` → `epic_guard.py`
→ `scheduler.gh` → `pulse_guard.gh`)."""

import sys

# ── Канонический текст bootstrap-блока (issue #723, доработка ревью PR #726,
# блокирующая находка 3) ─────────────────────────────────────────────────────
# Раньше гвардия (test_console_utf8_guard.py) проверяла только присутствие
# ПОДСТРОКИ "console_utf8" где угодно в файле — докстринг, комментарий вида
# «# TODO: подключить console_utf8 когда-нибудь» или блок со СКОПИРОВАННОЙ, но
# неверной глубиной пути проходили гвардию, ничего не подключая (доказано
# мутацией при ревью PR #726). Единственное место правды теперь — эти две
# константы; гвардия требует их ДОСЛОВНОГО присутствия в файле, выбирая
# ожидаемую форму по расположению файла: BOOTSTRAP_BLOCK_SAME_DIR — для точек
# входа внутри scripts/lib/ (console_utf8.py лежит рядом), BOOTSTRAP_BLOCK_
# PARENT_LIB — для всех остальных (scripts/<sub>/*.py, ровно один уровень
# вложенности ниже scripts/ — других форм в дереве на 2026-09-09 нет).
PATH_EXPR_SAME_DIR = 'parent / "console_utf8.py"'
PATH_EXPR_PARENT_LIB = 'parent.parent / "lib" / "console_utf8.py"'

_BOOTSTRAP_TEMPLATE = (
    "# --- console_utf8 bootstrap (класс: печать кириллицы валит encoding "
    "на Windows, issue #723) ---\n"
    "import importlib.util\n"
    "from pathlib import Path\n"
    "_console_utf8_spec = importlib.util.spec_from_file_location(\n"
    '    "console_utf8", Path(__file__).resolve().{path_expr})\n'
    "_console_utf8_spec.loader.exec_module(importlib.util.module_from_spec(_console_utf8_spec))\n"
    "# --- конец console_utf8 bootstrap ---\n"
)

BOOTSTRAP_BLOCK_SAME_DIR = _BOOTSTRAP_TEMPLATE.format(path_expr=PATH_EXPR_SAME_DIR)
BOOTSTRAP_BLOCK_PARENT_LIB = _BOOTSTRAP_TEMPLATE.format(path_expr=PATH_EXPR_PARENT_LIB)


def ensure_utf8_stdio() -> None:
    """Переключить sys.stdout/sys.stderr на UTF-8. Безопасно вызывать
    многократно и на не-Windows платформах (там stdio уже UTF-8 в
    подавляющем большинстве случаев — reconfigure на том же кодеке no-op)."""
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name, None)
        if stream is None or not hasattr(stream, "reconfigure"):
            continue  # поток подменён (тесты captured pytest) — не наш случай
        try:
            stream.reconfigure(encoding="utf-8", errors="backslashreplace")
        except (ValueError, OSError):
            # Поток не поддерживает reconfigure в этом состоянии (уже закрыт,
            # экзотическая обёртка) — не наше дело падать из-за косметики. Но
            # молчать нельзя (находка ревью PR #726, Н2): без явного следа
            # скрипт продолжит работу со strict-кодировкой локали, и «фикса
            # нет» станет неотличимо от «фикс есть, но не сработал». ASCII-
            # only — сам обработчик записи в этот поток, скорее всего, тоже
            # неисправен, кириллица в сообщении об этом отказе не поможет.
            print(
                f"console_utf8: WARNING - reconfigure(encoding=utf-8) failed for sys.{name} "
                "- this stream is still on the legacy locale codepage and may raise "
                "UnicodeEncodeError/UnicodeDecodeError on non-ASCII text (issue #723)",
                file=sys.__stderr__,
            )


ensure_utf8_stdio()
