#!/usr/bin/env python3
"""Сигнал дрейфа пина апстрима dsh-edge (#134).

Задача #134: сигнал «апстрим выпустил релиз новее пина» должен появляться
автоматически, чтобы бамп пина был осознанным действием по сигналу, а не
обнаружением владельцем. Проверка гипотезы из задачи «пульс уже сверяет
версии» показала: пульс НЕ сверял их никогда. Единственное сравнение версий
в системе живёт в DO морды (#73, cf-worker/src/harness.ts #checkDshEdgeUpdate):
npm-latest против задеплоенной /api/health, и при расхождении диспатчит
deploy-dsh-edge.yml — который при непустом манифесте пересобирает ЗАШИТЫЙ пин
(dsh-edge/upstream.json). Расхождение «релиз апстрима новее пина» не сравнивал
никто: каждые 4 часа зелёный деплой пересобирал тот же 0.7.1, и дрейф мог
висеть вечно. Эту сверку получает пульс оркестратора (этот модуль): сравнивать
надо то, что реально определяет версию морды — пин. Переустройство самого
механизма обновления (умный бамп по чейнджлогу) — задача #161, здесь только
сигнал.

Что делает каждый пульс:

  1. Читает пин из dsh-edge/upstream.json (форма {repo, sha} валидирует
     dsh-edge/manifest.mjs loadUpstream; здесь своя лёгкая проверка формы —
     сбой чтения не должен убить весь планировщик KeyError'ом).
  2. Берёт теги апстрима (repos/{repo}/tags — имена и sha одним запросом) и
     решает:
       ok          — пин стоит на теге новейшего СТАБИЛЬНОГО релиза;
       drift       — есть стабильный релиз семверно новее тега пина → сигнал;
       pin-not-tag — sha пина не на теге релиза: ни на каком теге, либо на
                     теге с нерелизным именем (алиас `stable`, `v0.8.0` —
                     не форма `dsh-edge-vX.Y.Z`; нарушение процедуры выбора
                     пина) → сигнал, никогда не падение.
  3. Сигнал «один раз на эпизод» — маркер-комментарий `[дрейф пина: <цель>]` в
     задаче #134: та же техника, что у предохранителя конвейера (#120) и
     авто-повторов ai-review (#196). Носитель — комментарии задачи: переживают
     перезапуск оркестратора. Цель эпизода — тег новейшего релиза (новый релиз
     апстрима = новая цель = новый сигнал) или `sha-<12>` для pin-not-tag.
     Известный размен: метку, снятую вручную ВО ВРЕМЯ активного эпизода, вернёт
     только новый сигнал (следующий релиз / другой пин) — мутирующий вызов на
     каждый пульс ради этого не делается.
  4. Каналы — общий канал пульса: комментарий в #134 + Telegram
     (pulse_guard.escalate) + метка `update-available`. Газ метки объявлен в
     docs/agents/LABELS.md и автоматичен: дрейф кончился (пин догнал апстрим) —
     этот же прогон снимает метку, вручную снимать не нужно.

Чистые решения (парсинг тегов, семвер, решение, тексты, маркеры) — здесь и
только здесь; gh/escalate — pulse_guard, второй реализации канала не заводим.
"""

import importlib.util
import json
import os
import re
import subprocess
from datetime import datetime
from pathlib import Path

import pulse_guard
from pulse_guard import escalate, issue_markers_any

# Пагинация списков (#308) — одно место правды, тот же приём, что уже
# использует scheduler.py (list_pages): review_labels.py лежит в scripts/lib,
# соседнем каталоге, не на sys.path при прямом запуске файла из
# scripts/orchestra — тот же явный file-spec импорт, не второй способ рядом.
_rl_spec = importlib.util.spec_from_file_location(
    "review_labels", Path(__file__).resolve().parents[1] / "lib" / "review_labels.py")
review_labels = importlib.util.module_from_spec(_rl_spec)
_rl_spec.loader.exec_module(review_labels)  # type: ignore[union-attr]

# Единственное место правды на заведение issue пула (#526): без него labels
# без `task` собирались бы копией той же проверки, что уже есть у
# file_tasks.py/scheduler.py/stall_detector.py — вместо этого все четверо
# зовут одну функцию.
_pi_spec = importlib.util.spec_from_file_location(
    "pool_issue", Path(__file__).resolve().parents[1] / "lib" / "pool_issue.py")
pool_issue = importlib.util.module_from_spec(_pi_spec)
_pi_spec.loader.exec_module(pool_issue)  # type: ignore[union-attr]

# gh зовётся через модуль (pulse_guard.gh), а не import-ом по имени: у
# тестов проводки должен быть ОДИН пункт патча — pulse_guard.gh, как уже
# устроено в test_scheduler.py (patch_gh). escalate/issue_markers_any внутри
# сами резолвят gh из глобов pulse_guard, поэтому патчатся тем же пунктом.

# Задача-статус дрейфа пина: сюда идут маркер-комментарии и метка (#134).
DRIFT_ISSUE = 134
DRIFT_LABEL = "update-available"
# Маркер-токен эпизода: закрывается целью, "[дрейф пина: dsh-edge-v0.9.0]".
# Скобочная форма нечаянно в прозе не пишется — как PAUSE_MARKER/HEARTBEAT_MARKER.
DRIFT_MARKER = "[дрейф пина:"

# Пин апстрима — единственное место правды о базе source-build морды.
PIN_PATH = Path(__file__).resolve().parents[2] / "dsh-edge" / "upstream.json"

# Форма релизного тега апстрима: dsh-edge-v0.8.0 (все 31 тег на 2026-09-03).
# Пререлиз — по семверу, дефис в версии (0.7.0-alpha.2): флаг prerelease из API
# релизов не нужен, теги дают и имя, и sha одним запросом.
_TAG_RE = re.compile(r"^dsh-edge-v(\d+)\.(\d+)\.(\d+)(-[0-9A-Za-z.-]+)?$")


def parse_release_tag(tag: str) -> tuple | None:
    """Полный семвер-ключ тега `dsh-edge-v0.8.0[-suffix]`; чужие теги → None.

    Возвращаемое значение сравнимо напрямую: больше — новее. Ядро (0, 8, 0)
    сравнивается покомпонентно как числа, поэтому (0, 10, 0) > (0, 9, 0).
    Стабильный релиз всегда новее пререлиза того же ядра (1.2.3 > 1.2.3-rc.1);
    внутри пререлиза числовой идентификатор младше строкового (semver §11:
    1.2.3-a.2 < 1.2.3-a.10 < 1.2.3-b)."""
    match = _TAG_RE.match(tag)
    if match is None:
        return None
    core = (int(match.group(1)), int(match.group(2)), int(match.group(3)))
    suffix = match.group(4)
    if not suffix:
        return (core, 1, ())
    parts = []
    for part in suffix[1:].split("."):
        if part.isdigit():
            parts.append((0, int(part), ""))
        else:
            parts.append((1, 0, part))
    return (core, 0, tuple(parts))


def tag_by_sha(tags: list[dict], sha: str) -> dict | None:
    """Тег, чей commit.sha равен sha пина. Прод-форма repos/{repo}/tags:
    [{name, commit: {sha, url}, zipball_url, tarball_url}]."""
    for tag in tags:
        if (tag.get("commit") or {}).get("sha") == sha:
            return tag
    return None


def latest_stable_tag(tags: list[dict]) -> dict | None:
    """Новейший СТАБИЛЬНЫЙ релизный тег. Пререлиз никогда не становится целью
    бампа: политика пина — «тег релиза» (dsh-edge/upstream.json), а пререлиз
    апстрим может переехать или снять. Нет ни одного стабильного тега — None."""
    candidates = []
    for tag in tags:
        key = parse_release_tag(tag.get("name", ""))
        if key is not None and key[1] == 1:
            candidates.append((key, tag))
    if not candidates:
        return None
    return max(candidates, key=lambda item: item[0])[1]


def decide_drift(pin: dict, tags: list[dict]) -> dict:
    """Решение по пину против релизов апстрима (три исхода — см. шапку модуля).

    pin — форма upstream.json {repo, sha}; tags — прод-форма repos/tags.
    Ни одного стабильного тега у апстрима — RuntimeError: сверять не с чем,
    и молчать об этом нельзя (тихий пропуск снова прячет дрейф)."""
    repo = pin["repo"]
    latest = latest_stable_tag(tags)
    if latest is None:
        raise RuntimeError(
            f"у {repo} не найдено ни одного стабильного релизного тега dsh-edge-v* — сверять пин не с чем")
    latest_key = parse_release_tag(latest["name"])
    pinned = tag_by_sha(tags, pin["sha"])
    # «Пин не на теге релиза» — ДВА разных случая, оба громкий pin-not-tag:
    # sha вообще не на теге ИЛИ на теге с нерелизным именем (алиас вида
    # `stable`/`v0.8.0`, не `dsh-edge-vX.Y.Z`). Второй случай обязан
    # классифицироваться, а не падать: сравнение None-ключа с ключом релиза
    # подняло бы TypeError, который обходит except RuntimeError обёртки
    # планировщика и роняет весь пульс (находка ревью крупного диффа #134).
    # На этом апстриме «два тега на одном коммите» уже случалось (v0.5.0 ==
    # v0.5.0-alpha.1), так что полагаться на порядок выдачи /tags нельзя.
    pinned_key = parse_release_tag(pinned["name"]) if pinned else None
    if pinned_key is None:
        return {
            "state": "pin-not-tag",
            "repo": repo,
            "sha": pin["sha"],
            "pinned_tag": pinned["name"] if pinned else None,
            "latest_tag": latest["name"],
        }
    # Пин может стоять и на пререлизном теге (сегодня — нет, но форма это
    # легальна): сравниваем ПОЛНЫМИ семвер-ключами, а не именами, чтобы пин на
    # 0.9.0-alpha.1 при стабильном 0.8.0 не кричал «отстаёт», а пин на rc того
    # же ядра, что вышедший стабильный, — кричал.
    state = "ok" if pinned_key >= latest_key else "drift"
    return {
        "state": state,
        "repo": repo,
        "sha": pin["sha"],
        "pinned_tag": pinned["name"],
        "latest_tag": latest["name"],
    }


def drift_target(decision: dict) -> str:
    """Цель эпизода — ключ «один сигнал на эпизод»: тег новейшего релиза
    (новый релиз апстрима = новая цель = новый сигнал) или sha-префикс для
    pin-not-tag (состояние привязано к конкретному пину)."""
    if decision["state"] == "pin-not-tag":
        return f"sha-{decision['sha'][:12]}"
    return decision["latest_tag"]


def last_drift_target(markers: list[tuple[datetime, str]]) -> str | None:
    """Цель последнего эпизода из тел маркеров `[дрейф пина: X]`. Без маркеров
    — None. Комментарии приходят по возрастанию времени — берём последний."""
    targets = []
    for _, body in markers:
        start = body.find(DRIFT_MARKER)
        if start == -1:
            continue
        end = body.find("]", start)
        if end == -1:
            continue
        targets.append(body[start + len(DRIFT_MARKER):end].strip())
    return targets[-1] if targets else None


def signal_pending(decision: dict, markers: list[tuple[datetime, str]]) -> bool:
    """True — этот эпизод ещё не сигналился: маркеров нет вовсе или цель
    последнего маркера не совпадает с текущей (новый релиз / новый пин)."""
    return last_drift_target(markers) != drift_target(decision)


def drift_alert_text(decision: dict) -> str:
    """Детерминированный текст сигнала. Первая строка — шапка канала
    (как pause_alert_text/heartbeat_alert_text в pulse_guard) с маркером
    эпизода: его тело ищет last_drift_target, содержание тестируется."""
    if decision["state"] == "pin-not-tag":
        if decision["pinned_tag"]:
            where = (f"тег {decision['pinned_tag']} — не релизная форма dsh-edge-vX.Y.Z "
                     "(алиас?)")
        else:
            where = "тега с таким sha у апстрима нет"
        head = (
            f"Пин апстрима ({decision['repo']}) стоит НЕ на теге релиза: {decision['sha']} — "
            f"{where}. Процедура выбора пина — sha тега релиза "
            "dsh-edge-vX.Y.Z (dsh-edge/upstream.json); source-build собирает непонятно что, "
            "и применимость патч-серии ничем не доказана."
        )
    else:
        head = (
            f"Апстрим {decision['repo']} выпустил {decision['latest_tag']}, пин морды держит "
            f"{decision['pinned_tag']} (dsh-edge/upstream.json). Морда отстаёт от апстрима: "
            "деплои пересобирают пин, сами версия не меняется — нужен осознанный бамп."
        )
    releases = f"https://github.com/{decision['repo']}/releases"
    return (
        f"🚨 edge-harness: {DRIFT_MARKER} {drift_target(decision)}]\n"
        f"{head}\n"
        f"Релизы апстрима: {releases}. Бамп — ручной PR: поднять sha в "
        "dsh-edge/upstream.json на тег релиза, перебазировать dsh-edge/patches "
        "(применимость доказывает шаг «Патч-серия» deploy-dsh-edge.yml), полный прогон "
        "деплоя с канарейками (dsh-edge/PATCHES.md). Швы бренда (#109), ingest (#119) и "
        "каталога моделей стоят гвардиями в деплое и громко скажут, если форма апстрима "
        "поменялась. Разбор чейнджлога перед бампом — задача #161 (умный бамп); этот "
        "сигнал фиксирует только факт дрейфа."
    )


def load_pin(pin_path: Path) -> dict:
    """Лёгкая проверка формы пина. Валидатор формы файла — dsh-edge/manifest.mjs
    loadUpstream; здесь проверка только чтобы сбой чтения был RuntimeError
    (планировщик его переживает), а не KeyError/JSONDecodeError на весь прогон."""
    try:
        pin = json.loads(pin_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise RuntimeError(f"{pin_path} не прочитан: {error}") from error
    sha = pin.get("sha") if isinstance(pin, dict) else None
    if (not isinstance(pin, dict)
            or not isinstance(pin.get("repo"), str)
            or not pin["repo"]
            or not isinstance(sha, str)
            or not re.fullmatch(r"[0-9a-f]{40}", sha)):
        raise RuntimeError(f"{pin_path}: ожидался пин с repo (строка) и sha (40 hex) — форма пина сломана")
    return pin


def _set_drift_label(repo: str) -> None:
    """Метка эпизода: ставится вместе с сигналом (один раз на эпизод)."""
    pulse_guard.gh("-X", "POST", f"repos/{repo}/issues/{DRIFT_ISSUE}/labels",
                   "-f", f"labels[]={DRIFT_LABEL}")


def _drop_drift_label(repo: str) -> None:
    """Газ метки (docs/agents/LABELS.md): дрейф кончился — снимаем сами.
    Метки нет — цель уже достигнута, DELETE не нужен (лишний мутирующий вызов
    на каждый пульс не делаем)."""
    issue = pulse_guard.gh(f"repos/{repo}/issues/{DRIFT_ISSUE}")
    if DRIFT_LABEL not in {label["name"] for label in issue.get("labels", [])}:
        return
    pulse_guard.gh("-X", "DELETE", f"repos/{repo}/issues/{DRIFT_ISSUE}/labels/{DRIFT_LABEL}")


# ── Автоматический бамп (#505): сигнал получает газ, не только предохранитель ────
#
# Дрейф кричал в #134 четыре эпизода подряд (0.9.0/0.10.0/0.11.0/0.11.1) без
# единого действия — тормоз без газа (AGENTS.md). Газ здесь — МЕХАНИЧЕСКАЯ
# половина бампа: поднять sha пина на тег новейшего стабильного релиза и
# открыть PR. Патч-серия `dsh-edge/patches` НЕ перебазируется автоматически:
# три минорных релиза апстрима меняли структуру ровно тех файлов, которые
# патчи трогают (живой пример #505 — `assemble-standalone-web.mjs` получил
# фазы boot-графа, `index.ts` лишился инлайн-выбора лимита тела запроса),
# и решить, что в патче — конфликт формы, а что — смысловая правка апстрима,
# может только суждение, не regex. Это ровно то, для чего задача #161 (умный
# бамп по чейнджлогу) остаётся отдельной, более амбициозной работой.
#
# Ворота после автоматического PR — деплой, а не ревью: `deploy-dsh-edge.yml`
# не гоняется на pull_request (dsh-edge/** триггерит его только push в main),
# поэтому `test`/`contract` пропустят PR, даже если патч-серия сломана — это
# честно названо в теле PR. Если апстрим сломал совместимость, деплой после
# мержа откажет ГРОМКО (шаг «Патч-серия», git apply без --check), и пульс
# отказов деплоя (#477) заведёт задачу на дефект — тот же класс сигнала, что
# уже есть для любого красного deploy-dsh-edge.yml, второй канал не заводится.
#
# Дедуп — единственно открытый PR с фиксированным префиксом заголовка. Пока
# такой PR открыт, новый не заводится («не плодить», требование задачи). Если
# PR закрыт БЕЗ мержа (человек взял перебазировку патчей на себя под своей
# задачей, как #505) — следующий пульс снова видит дрейф и откроет новую
# попытку; названный компромисс: автоматика не отличает «закрыт, потому что
# чинят руками» от «закрыт как неудачный», в обоих случаях путь свободен.
AUTO_BUMP_TITLE_PREFIX = "dsh-edge: авто-бамп пина апстрима до "


def open_auto_bump_pr(repo: str) -> dict | None:
    """Открытый PR предыдущей автоматической попытки, если он ещё жив.
    Пагинация (#308) через review_labels.list_pages — сырая первая страница
    молча теряла бы хвост при большом открытом пуле PR (тот же класс, что
    scheduler.py::open_pulls чинил тем же способом)."""
    pulls = review_labels.list_pages(f"repos/{repo}/pulls?state=open&per_page=100", pulse_guard.gh)
    for pull in pulls:
        if (pull.get("title") or "").startswith(AUTO_BUMP_TITLE_PREFIX):
            return pull
    return None


def bumped_pin_text(pin: dict, tag: dict, issue_number: int) -> str:
    """Новое содержимое upstream.json. Автоматическое происхождение названо
    прямо в note (человеческий бамп описывает перебазировку патчей — здесь
    её нет, и это не скрывается)."""
    return json.dumps({
        "repo": pin["repo"],
        "sha": tag["commit"]["sha"],
        "note": (
            f"Пин апстрима для source-build морды. Автоматический бамп (#{issue_number}, "
            f"scripts/orchestra/upstream_drift.py::attempt_auto_bump): sha — тег {tag['name']} "
            f"(repos/{pin['repo']}/tags). Патч-серия dsh-edge/patches НЕ перебазирована этим "
            "коммитом — применимость доказывает шаг «Патч-серия» deploy-dsh-edge.yml после "
            "мержа (dsh-edge/PATCHES.md); красный деплой — честный сигнал, не тихий пропуск "
            "версии, дальше заводит задачу пульс отказов деплоя (#477)."
        ),
    }, indent=2, ensure_ascii=False) + "\n"


def _run_git(args: list[str], *, cwd: Path) -> None:
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} упал: {result.stderr.strip()}")


def create_bump_issue(repo: str, decision: dict) -> int:
    """Заводит задачу пула под конкретный эпизод — контракт PR (branch
    agent/<N>-slug) требует открытой задачи с меткой task."""
    tag_name = decision["latest_tag"]
    body = (
        "## Цель\n"
        f"Пин `dsh-edge/upstream.json` поднят до `{tag_name}` "
        "(автоматически, scripts/orchestra/upstream_drift.py).\n\n"
        "## Критерий готовности\n"
        "- sha пина — коммит тега, патч-серия `dsh-edge/patches` накладывается подряд "
        "`git apply` без единого хунка мимо (шаг «Патч-серия» deploy-dsh-edge.yml).\n"
        "- Полный прогон deploy-dsh-edge.yml после мержа зелёный, `/api/health` отдаёт новую версию.\n\n"
        "## Площадь\narea:worker\n\n"
        "## Контекст и ссылки\n"
        "Автоматический бамп поднимает только sha пина, патч-серию не трогает: перебазировка "
        "патчей — суждение, не regex (см. докстринг attempt_auto_bump в upstream_drift.py). "
        "Если деплой после мержа красный — доведи вручную (перебазируй dsh-edge/patches), "
        "не подгоняй sha молча под то, что сходится.\n\n"
        "## Правила\n"
        "- [x] Я прочитал docs/research/30-rejected-alternatives.md и задача не из отвергнутых\n"
        "- [x] Критерий готовности проверяем по видимому результату\n"
    )
    created = pool_issue.create_pool_issue(
        pulse_guard.gh, repo, f"dsh-edge: пин апстрима отстаёт от {tag_name}", body,
        ["task", "area:worker"],
    )
    return created["number"]


def attempt_auto_bump(repo: str, decision: dict, tags: list[dict], *, pin_path: Path) -> str:
    """Одна попытка открыть авто-PR с поднятым пином. Никогда не бросает
    наружу — вызывающий (upstream_drift_check) уже внутри try/except границы
    сверки, но сбой ЗДЕСЬ не имеет права утопить уже отправленный сигнал
    дрейфа (комментарий/метка/Telegram выше по функции уже сработали)."""
    if decision["state"] != "drift":
        return "⚠️ авто-бамп не пытается чинить pin-not-tag — это вне его права, доводи вручную"
    pat = os.environ.get("ORCHESTRA_PAT")
    if not pat:
        return "⚠️ авто-бамп пропущен: ORCHESTRA_PAT не задан в окружении пульса"
    try:
        if open_auto_bump_pr(repo) is not None:
            return "🔁 авто-PR бампа уже открыт — новый не заводится"
        tag_obj = next((t for t in tags if t.get("name") == decision["latest_tag"]), None)
        if tag_obj is None:
            raise RuntimeError(f"тег {decision['latest_tag']} пропал из ответа /tags между решением и бампом")
        issue_number = create_bump_issue(repo, decision)
        branch = f"agent/{issue_number}-dsh-edge-upstream-bump"
        repo_root = pin_path.resolve().parents[1]
        _run_git(["fetch", "origin", "main"], cwd=repo_root)
        _run_git(["checkout", "-B", branch, "origin/main"], cwd=repo_root)
        pin_path.write_text(bumped_pin_text(load_pin(pin_path), tag_obj, issue_number), encoding="utf-8")
        _run_git(["add", str(pin_path.relative_to(repo_root))], cwd=repo_root)
        _run_git(["-c", "user.name=edge-harness-orchestra",
                  "-c", "user.email=orchestra@users.noreply.github.com",
                  "commit", "-m",
                  f"dsh-edge: авто-бамп пина апстрима до {decision['latest_tag']} (#{issue_number})"],
                 cwd=repo_root)
        _run_git(["-c", f"http.extraheader=AUTHORIZATION: bearer {pat}",
                  "push", "origin", f"HEAD:refs/heads/{branch}"], cwd=repo_root)
        pr_body = (
            f"Автоматический бамп пина апстрима (#{issue_number}). Патч-серия НЕ перебазирована "
            "этим PR — `deploy-dsh-edge.yml` (шаг «Патч-серия», post-merge) откажет громко, если "
            "форма апстрима поменялась; дальше пульс отказов деплоя (#477) заводит задачу на "
            "дефект. До мержа этот PR проходит только `test`/`contract` — ни один из них не "
            "проверяет применимость патчей, деплой на PR не гоняется."
        )
        # Единственный документированный путь открытия PR (#496,
        # docs/agents/PROTOCOL.md п.7) — обёртка над `gh pr create`, отказывает
        # ДО открытия PR на директиву Closes/Fixes/Resolves. Тело этого PR
        # такой директивы не несёт, но второй, необёрнутый путь открытия PR
        # рядом с уже документированным был бы вторым местом правды.
        pr_create = repo_root / "scripts" / "git" / "pr-create"
        result = subprocess.run(
            [str(pr_create), "--repo", repo, "--base", "main", "--head", branch,
             "--title", f"{AUTO_BUMP_TITLE_PREFIX}{decision['latest_tag']}", "--body", pr_body],
            capture_output=True, text=True, env={**os.environ, "GH_TOKEN": pat, "NO_COLOR": "1"},
        )
        if result.returncode != 0:
            raise RuntimeError(f"scripts/git/pr-create упал: {result.stderr.strip()}")
        return f"🤖 авто-бамп: открыт PR на задачу #{issue_number} ({result.stdout.strip()})"
    except RuntimeError as error:
        return f"⚠️ авто-бамп не удался: {error}"


def upstream_drift_check(repo: str, pin_path: Path = PIN_PATH) -> list[str]:
    """Проводка: один вызов из scheduler.main(). Возвращает строки отчёта пульса.

    Сбой сверки (сеть, API, форма пина) поднимается наверх как RuntimeError —
    вызывающий решает, как кричать. Тихий пропуск здесь сделал бы дрейф
    невидимым ровно так же, как его отсутствие до #134."""
    pin = load_pin(pin_path)
    tags = pulse_guard.gh(f"repos/{pin['repo']}/tags?per_page=100") or []
    # Гвардия усечения (ревью #134): страница полна — за кадром могут остаться
    # теги, включая новейший стабильный релиз, и сверка даст ЛОЖНЫЙ ok, то есть
    # молча спрячет дрейф. Громкий сбой лучше тихой неполности: планировщик
    # переживёт (⚠️ в отчёте), дрейф не потеряется.
    if len(tags) >= 100:
        raise RuntimeError(
            f"{pin['repo']}: /tags отдал полную страницу (100) — сверка может не видеть "
            "новейший релиз; нужна пагинация в upstream_drift_check")
    decision = decide_drift(pin, tags)

    if decision["state"] == "ok":
        # Газ: пока пин свеж, метки быть не должно; снимаем, если осталась от
        # прошлого эпизода (после мержа бампа).
        _drop_drift_label(repo)
        return [f"💗 пин апстрима свеж: {decision['pinned_tag']} — новейший стабильный релиз {decision['repo']}"]

    markers = issue_markers_any(repo, DRIFT_ISSUE, (DRIFT_MARKER,))
    if not signal_pending(decision, markers):
        # Второй раз на ту же цель не кричим: маркер уже стоит, метка стоит.
        return [f"🔇 дрейф пина {drift_target(decision)} уже сигналился в #{DRIFT_ISSUE} — повтор не шлём"]

    _set_drift_label(repo)
    delivered = escalate(repo, DRIFT_ISSUE, drift_alert_text(decision))
    lines = [f"🚨 дрейф пина: пин на {decision['pinned_tag'] or decision['sha'][:12]}, "
             f"апстрим выпустил {decision['latest_tag']} — сигнал в #{DRIFT_ISSUE} "
             f"+ метка {DRIFT_LABEL} ({delivered})"]
    lines.append(attempt_auto_bump(repo, decision, tags, pin_path=pin_path))
    return lines
