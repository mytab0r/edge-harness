#!/usr/bin/env python3
"""Тесты review_labels.py — единственного места правды для гейта слияния и
для выборочного подтягивания веток (#252).

fixtures_open_pulls_252.json — прод-форма, не пересказ: реальный ответ
`gh api "repos/mytab0r/edge-harness/pulls?state=open&per_page=100"` этого же
репозитория, снятый 2026-09-03 (после удаления собственного PR #252, чтобы
не засорять снимок). Поля сужены до number/draft/labels через `--jq`
(значения — те же самые, что вернул API, без пересказа) — полные объекты
несут в title/body случайные упоминания моделей из соседних задач, которые
ложно бьют guard'а #153 (стейл-провайдер/модель), а тесту здесь нужны только
labels. Тест разбирает то, что система реально отдаёт, а не наше
представление о формате.

Запуск: python -m pytest scripts/lib/test_review_labels.py -q
"""

import importlib.util
import json
from pathlib import Path

_DIR = Path(__file__).resolve().parent
SCRIPT = _DIR / "review_labels.py"
spec = importlib.util.spec_from_file_location("review_labels", SCRIPT)
review_labels = importlib.util.module_from_spec(spec)
spec.loader.exec_module(review_labels)  # type: ignore[union-attr]

FIXTURE = _DIR / "fixtures_open_pulls_252.json"


def _load_pulls() -> list[dict]:
    with open(FIXTURE, encoding="utf-8") as file:
        return json.load(file)


# ── Гвардия #252 на прод-форме: только близкие к слиянию или в конфликте ─────────
# Снимок содержал (2026-09-03): #162/#230/#231/#237 — метка conflict
# (подтягивать, может расшить); ни у одного открытого PR на момент снимка не
# было ОБОИХ вердиктов review:ok+ai:ok разом (см. отдельный синтетический
# тест ниже для этой ветки предиката) — #247/#248/#249/#253/#241/#181/#173 и
# другие стоят с review:large/ai:failed/ai:changes-requested/contract:failed
# без обоих зелёных (не подтягивать — дорогое AI-ревью и сброс вердикта без
# пользы для PR, которому рано сливаться).
EXPECTED_SHOULD_UPDATE = {162, 230, 231, 237}


def test_should_update_branch_matches_expected_on_real_open_pulls():
    pulls = _load_pulls()
    numbers = {pull["number"] for pull in pulls}
    # Гвардия свежести самого теста: если состав открытых PR в фикстуре
    # изменится (новый снимок), список ожиданий не должен молча протухнуть.
    assert numbers == {
        263, 262, 261, 260, 253, 249, 248, 247, 241, 237, 231, 230, 181, 173, 167, 162, 108,
    }

    should_update = {
        pull["number"] for pull in pulls
        if review_labels.should_update_branch(pull["labels"])
    }
    assert should_update == EXPECTED_SHOULD_UPDATE


def test_should_update_branch_true_only_for_conflict_or_both_verdicts():
    pulls = _load_pulls()
    for pull in pulls:
        names = {label["name"] for label in pull["labels"]}
        expected = ("conflict" in names) or (
            review_labels.REVIEW_OK in names and review_labels.AI_OK in names
        )
        actual = review_labels.should_update_branch(pull["labels"])
        assert actual == expected, f"#{pull['number']}: labels={sorted(names)}"


def test_should_update_branch_true_when_both_verdicts_green_without_conflict():
    # На снимке фикстуры такого PR нет (см. комментарий у EXPECTED_SHOULD_UPDATE) —
    # ветка предиката «близок к слиянию» проверяется синтетически, не прод-формой.
    labels = [{"name": "review:ok"}, {"name": "ai:ok"}]
    assert review_labels.should_update_branch(labels) is True


def test_should_update_branch_accepts_label_name_set_and_dict_list():
    # _names() поддерживает обе прод-формы: список dict'ов API и множество имён
    # (см. review_labels._names) — предикат обязан работать с обеими.
    dict_form = [{"name": "conflict"}]
    set_form = {"conflict"}
    assert review_labels.should_update_branch(dict_form) is True
    assert review_labels.should_update_branch(set_form) is True
    assert review_labels.should_update_branch([{"name": "review:ok"}]) is False
    assert review_labels.should_update_branch({"review:ok"}) is False


def test_should_update_branch_false_on_empty_labels():
    assert review_labels.should_update_branch([]) is False
    assert review_labels.should_update_branch(set()) is False


# ── Мутация гвардии: снять фильтр — тест обязан покраснеть ───────────────────────
# Доказательство держится руками в отчёте задачи #252 (правка should_update_branch
# на `return True` роняет test_should_update_branch_matches_expected_on_real_open_pulls
# и test_should_update_branch_true_only_for_conflict_or_both_verdicts), не тут:
# постоянная мутация в файле теста была бы сама по себе живым багом.


# ── Вердикт AI переживает подтягивание main без изменения диффа (#252) ───────
#
# fixtures_pr292_merge_diff.json — прод-форма: `gh api
# repos/mytab0r/edge-harness/compare/<merge-base>...<sha>` (сужено до
# filename/status/sha) для PR #292 ДО слияния main (голова — собственный
# коммит автора 659b518) и ПОСЛЕ (голова — merge-коммит 5432ce5,
# подтянувший main 2026-09-04). У PR #292 ровно два коммита: свой + один
# `Merge branch 'main'` без конфликтов — ровно тот сценарий, который чинит
# check_pr.py. Ответ идентичен побайтово (замерено `git diff --stat` между
# merge-base и головой на обеих точках, до и после слияния — 9 файлов, те
# же имена, те же blob-sha) — доказывает, что diff_fingerprint зависит
# только от содержимого патчей, а не от merge-коммита.
#
# fixtures_pr253_edit_diff.json — тот же приём для настоящей правки автора:
# PR #253 без единого merge-коммита (только собственные коммиты), rev1 —
# `gh api compare/<base>...4ad42edab9` (исходный пуш), rev2 — то же для
# следующего коммита `92f13f2863` (правка по ревью, убрала файл
# `openspec/specs/journal-tasks-hands.md` из диффа). Реальная правка меняет
# список файлов — отпечаток обязан отличаться.

FIXTURE_PR292 = _DIR / "fixtures_pr292_merge_diff.json"
FIXTURE_PR253 = _DIR / "fixtures_pr253_edit_diff.json"
FIXTURE_PR173 = _DIR / "fixtures_pr173_merge_diff.json"


def _load(fixture: Path, key: str) -> list[dict]:
    with open(fixture, encoding="utf-8") as file:
        return json.load(file)[key]


def test_diff_fingerprint_unchanged_across_clean_merge_from_main_pr292():
    before = _load(FIXTURE_PR292, "before_merge")
    after = _load(FIXTURE_PR292, "after_merge")
    assert review_labels.diff_fingerprint(before) == review_labels.diff_fingerprint(after)


def test_diff_fingerprint_changes_on_real_edit_pr253():
    rev1 = _load(FIXTURE_PR253, "rev1")
    rev2 = _load(FIXTURE_PR253, "rev2")
    assert review_labels.diff_fingerprint(rev1) != review_labels.diff_fingerprint(rev2)


# ── Критерий приёмки 5 issue #252: прод-форма — реальная история коммитов
# PR #173 (23-28 merge-коммитов с совпадающими таймстемпами) ─────────────────
#
# fixtures_pr173_merge_diff.json — `gh api repos/mytab0r/edge-harness/
# compare/main...<sha>` (сужено до filename/status/sha) для PR #173, который
# сама issue #252 назвала прод-примером (35 коммитов, 28 из них
# `Merge branch 'main'`, критерий 5 приводит таймстемп 2026-09-01T22:01:09Z —
# ровно этот merge-коммит и снят здесь). before_merge — голова ДО него
# (собственный коммит автора `ab99330d1b`, 2026-09-01T21:36:42Z), after_merge
# — сам merge-коммит (`425d8382e6`, 22:01:09Z), оба относительно merge-base с
# `main` (compare API считает его сам). Ответ идентичен побайтово (10 файлов,
# те же имена и blob-sha) — то же свойство, что и PR #292/#253 доказывают
# выше, но кормлено именно тем PR, который назвал критерий приёмки, а не
# каким-либо другим с тем же свойством.
def test_diff_fingerprint_unchanged_across_clean_merge_from_main_pr173_acceptance_criterion_5():
    before = _load(FIXTURE_PR173, "before_merge")
    after = _load(FIXTURE_PR173, "after_merge")
    assert review_labels.diff_fingerprint(before) == review_labels.diff_fingerprint(after)


def test_diff_fingerprint_order_independent():
    # Порядок страниц API не должен влиять — отпечаток сортирует список сам.
    files = _load(FIXTURE_PR292, "before_merge")
    assert review_labels.diff_fingerprint(files) == review_labels.diff_fingerprint(
        list(reversed(files)))


def test_diff_fingerprint_same_size_different_content_differs():
    # Класс, который явно назвала задача #252: разные правки одного размера
    # не должны случайно совпасть — blob-sha, не число строк, различает их.
    a = [{"filename": "f.py", "status": "modified", "sha": "aaa111"}]
    b = [{"filename": "f.py", "status": "modified", "sha": "bbb222"}]
    assert review_labels.diff_fingerprint(a) != review_labels.diff_fingerprint(b)


def test_diff_unchanged_requires_stored_fingerprint():
    # Нет сохранённого отпечатка (комментарий без diff:, старый формат) —
    # трактуется как «изменился»: сброс безопаснее ложного сохранения.
    assert review_labels.diff_unchanged(None, "abc") is False
    assert review_labels.diff_unchanged("", "abc") is False


def test_diff_unchanged_true_only_on_exact_match():
    assert review_labels.diff_unchanged("abc", "abc") is True
    assert review_labels.diff_unchanged("abc", "abd") is False


def test_header_facts_reads_diff_field():
    body = "pr: 292\nhead: 5432ce5\nreviewer: approve\ndiff: deadbeef\n\nпроза\n"
    assert review_labels.header_facts(body) == {
        "pr": "292", "head": "5432ce5", "reviewer": "approve", "diff": "deadbeef",
    }


def test_header_facts_diff_optional_backward_compat():
    # Старые комментарии (до этой правки) не несут diff: — шапка разбирается
    # как раньше, без KeyError и без выдумывания значения.
    body = "pr: 140\nhead: abc\nreviewer: rework\n\nпроза\n"
    facts = review_labels.header_facts(body)
    assert facts == {"pr": "140", "head": "abc", "reviewer": "rework"}
    assert "diff" not in facts


def test_latest_ai_comment_picks_last_reviewer_fact_paginated():
    # Две страницы, по 2 «факта-комментария» на страницу плюс шум без фактов —
    # latest_ai_comment обязан пролистать обе и вернуть последний по порядку
    # выдачи API (симулирует прод-форму file_tasks._pages/latest_review_comment).
    bot = {"login": "github-actions[bot]", "type": "Bot"}
    page1 = [
        {"body": "болтовня без фактов"},
        {"user": bot, "body": "pr: 1\nhead: a\nreviewer: rework\ndiff: fp-a\n"},
    ] + [{"body": f"шум {i}"} for i in range(98)]
    page2 = [{"user": bot, "body": "pr: 1\nhead: b\nreviewer: approve\ndiff: fp-b\n"}]

    calls: list[str] = []

    def fake_gh(url: str):
        calls.append(url)
        # Разбор точного параметра, не подстрока: "per_page=100" сам содержит
        # подстроку "page=1" — substring-матч тут же зациклил бы пагинацию
        # (тот самый класс, из-за которого file_tasks._pages бьёт постранично
        # через "&", а не строкой целиком).
        query = url.split("?", 1)[1] if "?" in url else ""
        params = dict(pair.split("=", 1) for pair in query.split("&") if "=" in pair)
        page = params.get("page")
        if page == "1":
            return page1
        if page == "2":
            return page2
        return []

    comment = review_labels.latest_ai_comment("o/r", 1, fake_gh)
    assert comment is not None
    facts = review_labels.header_facts(comment["body"])
    assert facts == {"pr": "1", "head": "b", "reviewer": "approve", "diff": "fp-b"}
    assert any("page=1" in call for call in calls) and any("page=2" in call for call in calls)


def test_latest_ai_comment_none_when_no_fact_comments():
    assert review_labels.latest_ai_comment(
        "o/r", 1, lambda url: [{"body": "просто обсуждение, без шапки"}]) is None


# ── Дыра безопасности (находка вердикта ai-review PR #294, её открыл наш же
# фикс #252): latest_ai_comment раньше доверяла ЛЮБОМУ автору шапки
# reviewer:. Репозиторий публичный — комментировать может кто угодно, и
# diff_fingerprint считается из публичного pulls/{n}/files, то есть
# вычислим посторонним. ──────────────────────────────────────────────────────

def test_latest_ai_comment_ignores_untrusted_author():
    # Комментарий постороннего (не github-actions[bot]) с валидной шапкой и
    # верным отпечатком не должен быть принят за вердикт AI.
    attacker = {
        "user": {"login": "random-outside-contributor", "type": "User"},
        "body": "pr: 294\nhead: fake\nreviewer: approve\ndiff: attacker-fp\n",
    }
    real = {
        "user": {"login": "github-actions[bot]", "type": "Bot"},
        "body": "pr: 294\nhead: real\nreviewer: rework\ndiff: real-fp\n",
    }
    comment = review_labels.latest_ai_comment(
        "o/r", 294, lambda url: [attacker, real] if "page=1" in url else [])
    assert comment is not None
    assert review_labels.header_facts(comment["body"])["diff"] == "real-fp"


def test_latest_ai_comment_none_when_only_untrusted_author():
    # Ни одного доверенного комментария нет вовсе — результат None, а не
    # подделка постороннего, пусть даже с идеальной шапкой.
    attacker = {
        "user": {"login": "random-outside-contributor", "type": "User"},
        "body": "pr: 294\nhead: fake\nreviewer: approve\ndiff: attacker-fp\n",
    }
    assert review_labels.latest_ai_comment(
        "o/r", 294, lambda url: [attacker] if "page=1" in url else []) is None


def test_is_trusted_verdict_author_requires_login_and_type():
    # Оба поля обязаны совпасть — ни login-подделка с чужим type, ни
    # совпавший type с другим login не признаются доверенными.
    assert review_labels._is_trusted_verdict_author(
        {"user": {"login": "github-actions[bot]", "type": "Bot"}}) is True
    assert review_labels._is_trusted_verdict_author(
        {"user": {"login": "github-actions[bot]", "type": "User"}}) is False
    assert review_labels._is_trusted_verdict_author(
        {"user": {"login": "someone-else", "type": "Bot"}}) is False
    assert review_labels._is_trusted_verdict_author({}) is False


# ── Пагинация pulls/{n}/files: класс «первая страница молча теряет хвосты»
# закрыт (находка вердикта ai-review PR #294) ────────────────────────────────
#
# fixtures_pr_over_100_files.json — СИНТЕТИЧЕСКАЯ фикстура: в репозитории на
# момент проверки (graphql changedFiles по всем 135 PR, 2026-09-05) нет PR
# с >100 изменённых файлов, максимум — 76 (#278). page1 (100 элементов) —
# склейка ДВУХ настоящих ответов `gh api pulls/{n}/files` (PR #278, 76 файлов
# + первые 24 из PR #10) — все поля реальные, склейка синтетическая (см.
# "_comment" внутри фикстуры). page2 ужат ревью-находкой (диффу этого PR
# нужен размер, не число хвостовых записей) до ДВУХ реальных записей того же
# снимка — этого достаточно, чтобы список files ушёл за границу первой
# страницы (100 → 102). page2_edited имитирует правку файла ЗА сотой
# позицией: последнему элементу page2 (scripts/orchestra/scheduler.py)
# присвоен sha другого РЕАЛЬНОГО файла того же снимка — не выдуманное
# значение.

FIXTURE_OVER100 = _DIR / "fixtures_pr_over_100_files.json"


def _load_over100(key: str) -> list[dict]:
    with open(FIXTURE_OVER100, encoding="utf-8") as file:
        return json.load(file)[key]


def _paged_gh(pages: dict[str, list[dict]]):
    """Fake gh(url) с точным разбором параметра page= (не подстрокой —
    "per_page=100" сам содержит "page=1", см. test_latest_ai_comment_*)."""
    def fake_gh(url: str):
        query = url.split("?", 1)[1] if "?" in url else ""
        params = dict(pair.split("=", 1) for pair in query.split("&") if "=" in pair)
        return pages.get(params.get("page"), [])
    return fake_gh


def test_list_pr_files_paginates_over_100_real_files_pr294():
    page1 = _load_over100("page1")
    page2 = _load_over100("page2")
    assert len(page1) == 100  # предпосылка сценария: первая страница ровно полная
    fake_gh = _paged_gh({"1": page1, "2": page2})
    files = review_labels.list_pr_files("o/r", 1, fake_gh)
    assert len(files) == len(page1) + len(page2)  # хвост за первой страницей не потерян
    assert {f["filename"] for f in files} == {f["filename"] for f in page1 + page2}


def test_list_pr_files_stops_on_short_page():
    fake_gh = _paged_gh({"1": [{"filename": "a", "status": "modified", "sha": "x"}]})
    files = review_labels.list_pr_files("o/r", 1, fake_gh)
    assert len(files) == 1


def test_list_pr_files_empty_on_no_files():
    assert review_labels.list_pr_files("o/r", 1, lambda url: []) == []


def test_diff_fingerprint_misses_tail_edit_without_pagination_pr294():
    # Класс, который явно назвала находка вердикта PR #294: ДОБАГОВЫЙ код
    # читал только ПЕРВУЮ страницу (files = gh(...per_page=100), без листания)
    # — правка файла на 101-й позиции была для отпечатка невидима.
    #
    # Эта проверка доказывает только половину класса — что ПОСЛЕ фикса
    # (полная пагинация, list_pr_files) правка в хвосте видна. Сравнение
    # «до фикса» через diff_fingerprint(page1) == diff_fingerprint(page1) —
    # тавтология (одинаковый вход даёт одинаковый выход у чистой функции
    # независимо от того, что она вычисляет) была здесь раньше и найдена
    # вердиктом ai-review PR #294 как ложная гвардия: она не исполняла ни
    # строки диагностируемого класса, только доказывала, что hash — чистая
    # функция. Настоящая защита от регресса «прод-код снова читает только
    # первую страницу» — grep-гвардии по исходнику:
    # test_check_pr.py::test_check_pr_reads_files_through_paginated_helper и
    # test_ai_review.py::test_ai_review_gather_and_verdict_read_files_through_paginated_helper
    # (обе проверяют текст вызова review_labels.list_pr_files, а не сырую
    # первую страницу, в самих check_pr.py/ai_review.py).
    page1 = _load_over100("page1")
    page2 = _load_over100("page2")
    page2_edited = _load_over100("page2_edited")
    assert page2 != page2_edited  # фикстура действительно содержит правку

    before_full = review_labels.diff_fingerprint(page1 + page2)
    after_full = review_labels.diff_fingerprint(page1 + page2_edited)
    assert before_full != after_full


def test_list_pr_files_fixes_added_undercount_pr294():
    # «Заодно это чинит занижение added в обоих гейтах» (находка вердикта
    # PR #294): сумма additions по одной странице меньше суммы по полному
    # списку ровно на additions хвостовых файлов.
    page1 = _load_over100("page1")
    page2 = _load_over100("page2")
    added_full = sum(f["additions"] for f in page1 + page2)
    added_first_page_only = sum(f["additions"] for f in page1)
    added_tail = sum(f["additions"] for f in page2)
    assert added_tail > 0
    assert added_full == added_first_page_only + added_tail
    assert added_full != added_first_page_only


# ── list_timeline: тот же класс пагинации, что list_pr_files, теперь на
# таймлайне issue/PR (#303, находка ревью) — last_review_ok_labeled_at и
# last_ready_labeled_at в scheduler.py читали сырую первую страницу
# timeline?per_page=100 без обхода: на PR с длинным таймлайном (много
# комментариев/пушей/перелейбловок) событие 'labeled' за первой сотней
# молча не находилось. ────────────────────────────────────────────────────────


def test_list_timeline_paginates_finds_event_beyond_first_page():
    # Докажи мутацией: замени `while True: ... page += 1` на однократный вызов
    # без обхода (сырой `gh_func(f"...timeline?per_page=100")`) — этот тест
    # покраснеет, потому что labeled-событие лежит на второй странице.
    page1 = [{"event": "commented", "created_at": "2026-08-01T00:00:00Z"} for _ in range(100)]
    page2 = [{"event": "labeled", "label": {"name": "review:ok"}, "created_at": "2026-09-01T00:00:00Z"}]
    fake_gh = _paged_gh({"1": page1, "2": page2})
    timeline = review_labels.list_timeline("o/r", 1, fake_gh)
    assert len(timeline) == len(page1) + len(page2)
    labeled = [e for e in timeline if e.get("event") == "labeled"]
    assert labeled and labeled[0]["created_at"] == "2026-09-01T00:00:00Z"  # событие за первой страницей найдено


def test_list_timeline_stops_on_short_page():
    fake_gh = _paged_gh({"1": [{"event": "commented", "created_at": "2026-08-01T00:00:00Z"}]})
    timeline = review_labels.list_timeline("o/r", 1, fake_gh)
    assert len(timeline) == 1


def test_list_timeline_empty_on_no_events():
    assert review_labels.list_timeline("o/r", 1, lambda url: []) == []


# ── list_pages: тот же класс, обобщённый на URL целиком (#308) ───────────────
#
# scheduler.open_task_issues/open_pulls читали сырую первую страницу списка
# (issues?state=open&labels=task&per_page=100 / pulls?state=open&per_page=100)
# без обхода — при 107 открытых задачах с меткой task (замер 2026-09-05, живой
# репозиторий) хвост за первой сотней был невидим воркеру и планировщику без
# ошибки и без предупреждения. reap_stale читал таймлайн issue той же сырой
# формой — тот же класс, что last_review_ok_labeled_at/last_ready_labeled_at
# (#303), сюда не мигрировали.

FIXTURE_TASK_ISSUES = _DIR / "fixtures_open_task_issues_308.json"


def _load_task_issues_pages() -> dict:
    with open(FIXTURE_TASK_ISSUES, encoding="utf-8") as file:
        return json.load(file)


def test_list_pages_paginates_over_100_real_open_task_issues_308():
    # Прод-форма: gh api repos/mytab0r/edge-harness/issues?state=open&labels=task
    # &per_page=100 (page=1) и page=2, снято 2026-09-05 — не пересказ.
    data = _load_task_issues_pages()
    page1, page2 = data["page1"], data["page2"]
    assert len(page1) == 100  # предпосылка сценария: первая страница ровно полная
    assert len(page2) > 0  # хвост существует — иначе тест ничего не доказывает
    fake_gh = _paged_gh({"1": page1, "2": page2})
    items = review_labels.list_pages(
        "repos/mytab0r/edge-harness/issues?state=open&labels=task&per_page=100", fake_gh)
    # Мутация: замени `while True: ...` на однократный `gh_func(url)` без
    # обхода — этот тест покраснеет (len(items) упадёт до 100).
    assert len(items) == len(page1) + len(page2)
    assert {i["number"] for i in items} == {i["number"] for i in page1 + page2}


def test_list_pages_stops_on_short_page():
    fake_gh = _paged_gh({"1": [{"number": 1}]})
    items = review_labels.list_pages("repos/o/r/issues?state=open&per_page=100", fake_gh)
    assert len(items) == 1


def test_list_pages_empty_on_no_items():
    assert review_labels.list_pages("repos/o/r/issues?state=open&per_page=100", lambda url: []) == []


# ── should_run_ai_review: дорогой прогон второго гейта переживает
# подтягивание main, но не отнимает газ #196 у ai:failed (находка вердикта
# ai-review PR #294) ─────────────────────────────────────────────────────────

def test_should_run_ai_review_skips_on_final_verdict_and_unchanged_diff():
    assert review_labels.should_run_ai_review([{"name": "ai:ok"}], "fp", "fp") is False
    assert review_labels.should_run_ai_review(
        [{"name": "ai:changes-requested"}], "fp", "fp") is False


def test_should_run_ai_review_runs_when_diff_changed():
    assert review_labels.should_run_ai_review([{"name": "ai:ok"}], "fp-old", "fp-new") is True
    assert review_labels.should_run_ai_review([{"name": "ai:ok"}], None, "fp-new") is True


def test_should_run_ai_review_runs_when_no_final_verdict_yet():
    assert review_labels.should_run_ai_review([], None, "fp") is True
    assert review_labels.should_run_ai_review([{"name": "review:ok"}], None, "fp") is True


def test_should_run_ai_review_never_skips_ai_failed_even_with_matching_fingerprint():
    # Газ #196 (scheduler.trigger_ai_review — автоповтор ai:failed по таймеру)
    # не должен гаситься совпавшим отпечатком: ai:failed всегда «нужен прогон».
    assert review_labels.should_run_ai_review([{"name": "ai:failed"}], "fp", "fp") is True


def test_should_run_ai_review_mutation_guard_naive_any_verdict_check():
    # Мутационная проверка (AGENTS.md, «доказано мутацией»): наивная реализация
    # «есть любой ai:*-вердикт (bool(ai_verdicts_to_drop)) И дифф не менялся —
    # пропустить» (ровно то, чем сейчас пользуется check_pr.ai_verdict_keep для
    # РЕШЕНИЯ О МЕТКЕ, но НЕ годится для решения о запуске прогона) молча
    # накрыла бы и ai:failed — тогда его автоповтор по таймеру #196 перестал
    # бы срабатывать, если пуш пришёл с тем же диффом (например, ретрай через
    # workflow_dispatch на неизменном коде). should_run_ai_review обязан
    # отличаться от этой наивной формы именно в этой точке.
    naive_would_skip = bool(review_labels.ai_verdicts_to_drop([{"name": "ai:failed"}])) and \
        review_labels.diff_unchanged("fp", "fp")
    assert naive_would_skip is True  # «наивная» реализация пропустила бы прогон
    assert review_labels.should_run_ai_review([{"name": "ai:failed"}], "fp", "fp") is True  # фикс — нет
