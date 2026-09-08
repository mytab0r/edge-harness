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
            # экзотическая обёртка) — не наше дело падать из-за косметики.
            pass


ensure_utf8_stdio()
