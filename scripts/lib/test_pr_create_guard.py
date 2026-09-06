#!/usr/bin/env python3
"""scripts/git/pr-create (issue #496) — гвардия проводки, не переизобретённой
логики: обёртка обязана звать РОВНО `task_ref.closing_keyword_refs` (то же
единственное место правды, что и `scripts/orchestra/contract_check.py`), а не
держать второй regex рядом. Поведенческие сценарии (директива отклонена ДО
вызова gh, backtick-упоминание пропущено) — scripts/git/test/pr-create-guard.
test.sh, это гоняет реальный bash-процесс на настоящем python3.

Мутация: замени `task_ref.closing_keyword_refs` на самодельный regex в
scripts/git/pr-create — test_wrapper_calls_shared_detector краснеет (нет
больше ссылки на общую функцию), а pr-create-guard.test.sh рискует разойтись
с contract_check.py по семантике незаметно.

Запуск: python -m pytest scripts/lib/test_pr_create_guard.py -q
"""

import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
PR_CREATE = REPO_ROOT / "scripts" / "git" / "pr-create"
CONTRACT_CHECK = REPO_ROOT / "scripts" / "orchestra" / "contract_check.py"


def _non_comment_lines(text: str) -> str:
    # Верхний блок-комментарий файла (докстринг в стиле bash, строки с #)
    # тоже упоминает task_ref.closing_keyword_refs текстом — substring-проверка
    # по ВСЕМУ файлу прошла бы даже после удаления настоящего вызова из кода
    # (живая находка при разработке этого теста: мутация «вырезать блок
    # проверки» оставляла комментарий и не красила тест). Фильтр по
    # незакомментированным строкам делает проверку про КОД, а не про прозу.
    return "\n".join(
        line for line in text.splitlines() if not line.strip().startswith("#")
    )


def test_wrapper_calls_shared_detector_not_a_copy():
    code = _non_comment_lines(PR_CREATE.read_text(encoding="utf-8"))
    assert "task_ref.closing_keyword_refs" in code, (
        "scripts/git/pr-create не зовёт task_ref.closing_keyword_refs В КОДЕ "
        "(не только в комментарии) — единственное место правды на директиву "
        "Closes/Fixes/Resolves (issue #496, тот же вызов, что "
        "scripts/orchestra/contract_check.py); переизобретённый regex здесь "
        "неминуемо разойдётся с контрактом"
    )


def test_contract_still_uses_the_same_function():
    # Симметричная сторона гвардии: если contract_check.py перестанет звать
    # closing_keyword_refs (переименование/рефакторинг), обёртка это не
    # заметит сама по себе — тест ловит расхождение с обеих сторон.
    text = CONTRACT_CHECK.read_text(encoding="utf-8")
    assert "task_ref.closing_keyword_refs" in text, (
        "scripts/orchestra/contract_check.py больше не зовёт "
        "task_ref.closing_keyword_refs — обнови scripts/git/pr-create на "
        "актуальное имя функции (issue #496)"
    )


def test_wrapper_is_executable_in_git_index():
    # Бит исполнения читаем из индекса git (`git ls-files -s`), а не из
    # os.stat: на Windows-дереве разработки NTFS/git-bash не гарантируют
    # тот же бит, что увидит Linux-раннер CI при чекауте — портируемая
    # проверка смотрит именно на то, что реально уедет в репозиторий.
    result = subprocess.run(
        ["git", "ls-files", "-s", "scripts/git/pr-create"],
        cwd=REPO_ROOT, capture_output=True, text=True, check=True,
    )
    mode = result.stdout.split()[0] if result.stdout.strip() else ""
    assert mode == "100755", (
        f"scripts/git/pr-create в индексе git имеет режим {mode!r}, ожидался "
        "100755 (исполняемый) — git update-index --chmod=+x scripts/git/pr-create"
    )
