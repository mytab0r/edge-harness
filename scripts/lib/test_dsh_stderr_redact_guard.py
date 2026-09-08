#!/usr/bin/env python3
"""Гвардия класса «сырой stderr/stdout клиента модели уезжает в лог без
redact()» (#743, живая утечка — DSH_CHAIN_CLASS_NOTE, scripts/lib/dsh-ci.sh:264,
до фикса).

Класс: репозиторий публичный, GitHub маскирует только точное совпадение со
значением секрета — производное (base64, подстрока, эхо заголовка
Authorization) не маскируется (AGENTS.md, «Секреты»). Ошибка клиента модели
(dsh) — самое вероятное место, куда в лог мог бы уехать производный
DEEPSEEK_API_KEY (обоснование redact() в scripts/review/ai_dsh.sh:112).
Соседние места этого же класса уже маскируют осознанно: ai_dsh.sh (`tail …|
redact`), hands/dsh_task.sh и worker/task.sh (`ERRTEXT=$(tail … "$ERR_FILE" |
redact)` / `ERR_TAIL=$(tail … "$ANSWER_FILE"|"$ERR_FILE" | redact)`) — только
dsh-ci.sh::dsh_chain_should_advance брал сырой хвост без пары.

Признак: команда, извлекающая СОДЕРЖИМОЕ файла (cat/tr/cut/head/tail/sed/
fold/base64/xxd — не grep/wc/test, они не публикуют содержимое, только факт),
применённая к переменной/пути, чьё имя называет stderr/answer клиента модели
(err_file, ERR_FILE, stderr.txt, answer_file, ANSWER_FILE, answer.txt) —
БЕЗ `redact` дальше в той же командной цепочке (тот же физической строке,
как и оформлены все существующие примеры в этом репозитории — и безопасные,
и вылеченная утечка).

Проверка позиционная (команда-извлечения обязана предшествовать имени файла
в пределах одного pipe-сегмента), поэтому не путает случайное соседство в
строке (например вызов `dsh_run_with_provider_chain "$X/answer.txt" ... "$(cat
"$X/prompt.md")"` — cat здесь читает НЕ answer.txt, позиционно после `cat`
не стоит имя файла из списка).

Тесты, смоук-фикстуры (scripts/lib/test/**, файлы test_*.py) исключены —
там либо синтетические, либо целиком застабленные данные (сами являются
частью механики проверки, не продовым транспортом).

Запуск: python -m pytest scripts/lib/test_dsh_stderr_redact_guard.py -q
"""

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

CONTENT_CMDS = r"(?:cat|tr|cut|head|tail|sed|fold|base64|xxd)"
FILE_MARKERS = r"(?:err_file|ERR_FILE|stderr\.txt|answer_file|ANSWER_FILE|answer\.txt)"

# Команда-извлечения, затем (в пределах сегмента без `|`) имя файла-маркера
# класса — та самая позиционная связь «команда читает именно этот файл».
SOURCE_STAGE_RE = re.compile(
    rf"\b{CONTENT_CMDS}\b(?:(?!\||\$\().)*?\b{FILE_MARKERS}\b"
)

REDACT_RE = re.compile(r"\bredact\b")

# Осознанно разрешённые исключения (сейчас пусто — каждый существующий
# случай уже проходит редактом на той же строке). Ключ — (относительный
# путь, номер строки, 1-индекс) → причина.
ALLOWED_UNREDACTED: dict[tuple[str, int], str] = {}


def _production_scripts() -> list[Path]:
    """.sh-скрипты scripts/, кроме test/-каталога и *.guard.sh/*.smoke.sh —
    там либо синтетические фикстуры, либо сама механика проверки."""
    out = []
    for path in (REPO_ROOT / "scripts").rglob("*.sh"):
        rel = path.relative_to(REPO_ROOT / "scripts")
        parts = rel.parts
        if "test" in parts:
            continue
        if path.name.endswith((".guard.sh", ".smoke.sh")):
            continue
        out.append(path)
    return out


def _find_offenders() -> list[str]:
    offenders = []
    for path in _production_scripts():
        rel = str(path.relative_to(REPO_ROOT)).replace("\\", "/")
        lines = path.read_text(encoding="utf-8").splitlines()
        for idx, line in enumerate(lines, start=1):
            if (rel, idx) in ALLOWED_UNREDACTED:
                continue
            # Каждый '|'-сегмент строки проверяется отдельно: redact()
            # обязан стоять ДАЛЬШЕ в том же pipe-конвейере, что и источник.
            segments = line.split("|")
            for seg_idx, segment in enumerate(segments):
                if not SOURCE_STAGE_RE.search(segment):
                    continue
                rest = "|".join(segments[seg_idx:])
                if not REDACT_RE.search(rest):
                    offenders.append(f"{rel}:{idx} — {line.strip()}")
                break
    return offenders


def test_no_unredacted_model_client_output():
    offenders = _find_offenders()
    assert offenders == [], (
        "Сырое содержимое stderr/stdout клиента модели (dsh) читается "
        "командой-извлечения БЕЗ redact() в том же конвейере (класс #743 — "
        "GitHub маскирует только точное совпадение секрета, производное "
        "(эхо заголовка Authorization) — нет): "
        f"{offenders}. Пропусти через redact() (scripts/lib/dsh-ci.sh, "
        "функция redact) в том же pipe-конвейере, по образцу "
        "scripts/review/ai_dsh.sh:112 / scripts/hands/dsh_task.sh::ERRTEXT / "
        "scripts/worker/task.sh::ERR_TAIL. Если новое место осознанно "
        "безопасно (содержимое заведомо не может нести секрет) — назови "
        "причину и добавь запись в ALLOWED_UNREDACTED этого файла."
    )


# Мутация, которой доказана гвардия (#743): в scripts/lib/dsh-ci.sh верни
#   DSH_CHAIN_CLASS_NOTE="$(tr '\n' ' ' <"$err_file" | cut -c1-200)"
# (убери `| redact` из конца) — этот тест красный. Верни `| redact` — тест
# снова зелёный. (Тем же приёмом доказана поведенческая гвардия
# scripts/lib/test/dsh-provider-chain.smoke.sh, сценарий 8.)
