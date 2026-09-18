#!/usr/bin/env python3
"""Применение решения владельца, присланного нажатием инлайн-кнопки в Telegram
(#254). Запускается workflow'ом .github/workflows/owner-decision.yml по
repository_dispatch (event_type owner-decision), который морда шлёт узким
GH_DISPATCH_TOKEN (ADR 0008, Contents+Actions, без Issues). Этот job читает
ОДИН секрет репозитория — TELEGRAM_WEBHOOK_SECRET (проверка подписи ниже;
до правки #1251 секретов не читал вовсе, прежняя формулировка этого
докстринга «никакого нового секрета» описывала то прежнее поведение и
противоречила новому тексту ниже — модуль противоречил сам себе,
исправлено ревью PR #1254), — плюс issues:write из
permissions workflow'а (github.token).

Пишет РОВНО ТОТ ЖЕ артефакт, что и ручной ответ владельца комментарием
(#470/#471, PROTOCOL.md «Решение владельца как артефакт») — первая строка
«РЕШЕНИЕ: N» в задаче issue_number. Дальше решение снимет waiting:owner
гвардия scripts/orchestra/waiting_owner_guard.py (#470/#471, слит) на
следующем пульсе orchestra. Второй путь применения здесь НЕ заводится,
переиспользован post_issue_comment из pulse_guard.py без изменений.

Подпись client_payload (issue #1251, разбор от 2026-09-14): repository_dispatch
не проверяет, каким токеном его вызвали, только scope — любой держатель
токена с правом Actions-write на этот репозиторий (это есть у каждого
агента) вызывает `POST /repos/{owner}/{repo}/dispatches` напрямую с
произвольным client_payload и получает ОТ ЭТОГО job'а тот же артефакт
«РЕШЕНИЕ: N», что и настоящее нажатие кнопки. Заслон — HMAC-SHA256 над
`{issue_number}:{option}` секретом TELEGRAM_WEBHOOK_SECRET. Секрет
существующий: уже аутентифицирует бота вебхука на стороне морды
(#ownerChatAuthorized/#telegramWebhookAuthorized в cf-worker/src/harness.ts)
и уже лежит в GitHub Actions repository secrets тем же именем
(deploy-worker.yml/telegram-webhook.yml читают его как
`secrets.TELEGRAM_WEBHOOK_SECRET`) — подпись НЕ заводит нового секрета, а
даёт существующему ВТОРУЮ роль (ключ решения владельца). Это расширение
принято ЯВНО, не молча: принцип ADR 0014 «у секрета — одна роль» здесь
нарушен сознательно, с названной ценой (секрет известен третьей стороне —
Telegram через setWebhook, — и компрометация роли вебхука каскадом открывает
роль подписи; класс «секрет-шире-имени», находка ревью PR #1254) и условием
возврата — выделенный секрет подписи (ADR 0014, «Изменение 2026-09-17»).
Подпись при этом — НЕ изоляция от агентов: Actions-копию секрета читает
любой слитый workflow, а артефакт и так подделываем дешевле комментарием
«РЕШЕНИЕ: N» (принятый риск #1251) — назначение подписи: происхождение
артефакта («Источник: нажатие инлайн-кнопки») не врёт и цена подделки
артефакта-кнопки выше, чем у комментария. `verify_signature` даёт
ТРИ РАЗНЫХ отказа, ни один не пишет комментарий (класс #1096, носитель
третьего состояния scripts/lib/check_result.py — здесь применена та же
дисциплина «не схлопывать разные причины в одно НЕТ», без прямого импорта
модуля: тот заточен под проверки инвариантов репозитория, эта — под
верификацию подписи одного payload'а; не-ASCII подпись — не четвёртый
отказ, а обычный mismatch, сравнение байтовое):
  1. TELEGRAM_WEBHOOK_SECRET не настроен в этом job'е — подпись НЕЧЕМ
     проверить (`GithubSecretMissing`);
  2. client_payload не несёт подписи вовсе (пустая строка — воркер её
     отправляет уже сейчас всегда, см. #dispatchOwnerDecision, пустая может
     прийти либо от воркера без TELEGRAM_WEBHOOK_SECRET, либо от воркера
     старше этого мержа, см. «переходный режим» ниже) (`SignatureMissing`);
  3. подпись есть, но не совпала с ожидаемой — подделка или испорченный
     payload (`SignatureMismatch`).
Повтор ТОГО ЖЕ (issue, option) сознательно НЕ гвардируется нонсом здесь:
повтор валидной подписи произвёл бы тот же самый текст «РЕШЕНИЕ: N» (не
другое решение), а «протухшая кнопка другого раунда» — отдельный,
существовавший ДО этой правки пробел. `issue_still_waiting` ниже закрывает
из него случай «метка снята или задача закрыта»; остаточный кейс назван
ОТКРЫТЫМ, не закрыт: метку waiting:owner поставили ЗАНОВО вручную под новый
раунд (`gh issue edit --add-label` — так же её ставит любой канал), старая
валидная подпись ТОЙ ЖЕ пары (issue, option) остаётся валидной и запишет
старый смысл за новую нумерацию вариантов. Нонс/привязка подписи к раунду —
отдельная работа, не часть этой правки.

Переходный режим (issue #1251, критерий 4): PR правит воркер и workflow
ОДНИМ коммитом на `cf-worker/**`, поэтому один push на main запускает
`deploy-worker.yml` (path-фильтр `cf-worker/**`) — окно, где
`owner-decision.yml` уже проверяет подпись, а Cloudflare-воркер ещё старой
версии (не подписывает), существует РОВНО длительность прогона
`deploy-worker.yml` (минуты) и закрывается САМ, без ручного шага: как
только деплой заканчивается, воркер уже шлёт подпись всегда. Внутри этого
окна нажатие кнопки красит job (SignatureMissing) — это видимый, громкий,
самостоятельно устраняющийся сбой, не тихая дыра: постоянного «мягкого»
режима, принимающего пустую подпись бессрочно, здесь нет и не будет —
именно бессрочный обход и был дырой issue #1251.

Проверка «задача всё ещё ждёт» (находка ревью PR #486, четвёртый заход):
клавиатуры прошлых эскалаций ничем не удаляются (кнопки снимает только
`editMessageText` по нажатию), а гвардия шлёт НОВОЕ кнопочное сообщение
каждые `WAITING_OWNER_REESCALATE_HOURS` часов — после ответа комментарием
в чате несколько наборов живых кнопок остаются нажимаемыми. Случайное
нажатие протухшей кнопки без этой проверки записало бы «РЕШЕНИЕ: N» в
задачу, уже взятую воркером, закрытую, или переоткрытую под новый раунд с
другой нумерацией вариантов — молча неверный артефакт, выглядящий
авторитетным решением владельца. Границы проверки названы честно: она
закрывает случай «метка снята или задача закрыта»; кейс «метку поставили
заново руками под новый раунд» ею НЕ закрыт (старая подпись той же пары
issue+option остаётся валидной — см. выше), это открытое остаточное
ограничение, а не закрытый риск. Морда к этому моменту уже ответила
владельцу «Принято» (answerCallbackQuery по 204 от dispatch) — красный job
здесь единственный видимый сигнал расхождения, поэтому отказ громкий
(RuntimeError → `::error::` → exit 1), не тихий no-op.

Ручной запуск с ноута — ИЗМЕНЕНИЕ КОНТРАКТА ЭТИМ МЕРЖЕМ (#1251; находитка
ревью PR #1254): #1037 называет «ручной apply_owner_decision с ноута»
легитимным PAT-путём, и до этой правки вызов без аргумента подписи писал
«РЕШЕНИЕ: N» безусловно. Теперь тот же вызов требует env
TELEGRAM_WEBHOOK_SECRET (тот же секрет, что в GitHub Actions secrets и
Cloudflare — есть ли он на ноуте, НЕ подтверждено) и --signature со
значением compute_signature(secret, issue, option); без них скрипт откажет
первыми двумя отказами verify_signature выше, НЕ записав ничего — это
сознательно, безусловный обход подписи был бы той же дырой, что и прямой
dispatch. Владелец без секрета на ноуте применяет решение нажатием кнопки
в Telegram (аутентифицированный путь) либо комментарием «РЕШЕНИЕ: N».

Запуск: python scripts/orchestra/apply_owner_decision.py --repo o/r --issue N --option M
"""

# --- console_utf8 bootstrap (класс: печать кириллицы валит encoding на Windows, issue #723) ---
import importlib.util
from pathlib import Path
_console_utf8_spec = importlib.util.spec_from_file_location(
    "console_utf8", Path(__file__).resolve().parent.parent / "lib" / "console_utf8.py")
_console_utf8_spec.loader.exec_module(importlib.util.module_from_spec(_console_utf8_spec))
# --- конец console_utf8 bootstrap ---

import argparse
import hashlib
import hmac
import os
import sys

from pulse_guard import DECISION_COMMENT_PREFIX, gh, post_issue_comment
from waiting_owner_guard import WAITING_OWNER_LABEL

# Имя переменной окружения, несущей секрет подписи (#1251) — ОДНО место
# правды и здесь, и в .github/workflows/owner-decision.yml (env: с тем же
# именем из secrets.TELEGRAM_WEBHOOK_SECRET), и в cf-worker/src/harness.ts
# (env.TELEGRAM_WEBHOOK_SECRET) — три файла читают/пишут один секрет по
# одному имени, расхождение имени сделало бы подпись непроверяемой молча.
SIGNATURE_SECRET_ENV_VAR = "TELEGRAM_WEBHOOK_SECRET"


def decision_comment(option: int) -> str:
    return (
        f"{DECISION_COMMENT_PREFIX}: {option}\n\n"
        "Источник: нажатие инлайн-кнопки в Telegram (#254)."
    )


def compute_signature(secret: str, issue_number: int, option: int) -> str:
    """HMAC-SHA256(secret, "issue_number:option") — тот же формат, что
    #dispatchOwnerDecision считает в cf-worker/src/harness.ts (#hmac над
    `${issueNumber}:${option}`, вторым аргументом TELEGRAM_WEBHOOK_SECRET).
    Изменение формата ОБЯЗАНО меняться синхронно в обоих местах — иначе
    подпись живого воркера перестаёт проверяться этим job'ом."""
    message = f"{issue_number}:{option}".encode("utf-8")
    return hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()


def verify_signature(secret: str | None, issue_number: int, option: int, signature: str | None) -> None:
    """Три РАЗНЫХ отказа (issue #1251, класс #1096 — не схлопывать причины в
    одно НЕТ), ни один не должен привести к post_issue_comment: секрет не
    настроен в этом job'е, подписи нет в client_payload, подпись не
    совпала (включая не-ASCII подделку — она mismatch, не TypeError,
    сравнение байтовое). См. докстринг модуля, раздел «Подпись
    client_payload», за объяснением каждой причины и переходного режима."""
    if not secret:
        raise RuntimeError(
            f"секрет {SIGNATURE_SECRET_ENV_VAR} не настроен в этом job'е (GitHub Actions "
            "repository secret) — подпись НЕЧЕМ проверить, решение НЕ записано"
        )
    if not signature:
        raise RuntimeError(
            "client_payload не несёт подписи (signature пусто) — dispatch либо от "
            "воркера без TELEGRAM_WEBHOOK_SECRET/старее правки #1251 (переходное окно "
            "деплоя, самоустраняется), либо вызван напрямую в обход кнопки Telegram; "
            "решение НЕ записано"
        )
    expected = compute_signature(secret, issue_number, option).encode("utf-8")
    # Сравнение БАЙТАМИ, не строками (находка ревью PR #1254): строковый
    # hmac.compare_digest бросает TypeError на не-ASCII — подделка вида
    # --signature "подпись" падала бы сырым трейсбеком ЧЕТВЁРТЫМ,
    # не каталогизированным отказом, мимо форматирования ::error::
    # (ловится только RuntimeError). str.encode("utf-8") не бросает
    # никогда, байтовый compare_digest даёт обычный mismatch выше.
    supplied = signature.encode("utf-8")
    if not hmac.compare_digest(expected, supplied):
        raise RuntimeError(
            f"#{issue_number}: подпись client_payload не совпадает с ожидаемой для "
            f"варианта {option} — подделка, испорченный payload или подпись от другой "
            "пары issue/option; решение НЕ записано"
        )


def issue_still_waiting(repo: str, issue_number: int) -> bool:
    """Задача открыта И всё ещё несёт waiting:owner — решение по нажатой
    кнопке применимо. False — задача закрыта, или метку уже сняли/сменили
    раунд (см. докстринг модуля)."""
    issue = gh(f"repos/{repo}/issues/{issue_number}")
    if not issue or issue.get("state") != "open":
        return False
    labels = {label["name"] for label in (issue.get("labels") or [])}
    return WAITING_OWNER_LABEL in labels


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True, help="owner/repo")
    parser.add_argument("--issue", required=True, type=int, help="номер задачи (issue), не PR")
    parser.add_argument("--option", required=True, type=int, help="номер выбранного варианта (с 1)")
    parser.add_argument(
        "--signature", default="",
        help="HMAC-подпись client_payload (#1251) из github.event.client_payload.signature; "
             "для ручного запуска значение считает compute_signature(secret, issue, option), "
             "secret — env SIGNATURE_SECRET_ENV_VAR (см. докстринг модуля, «Ручной запуск "
             "с ноута»); пусто — трактуется как «подписи нет» в verify_signature",
    )
    args = parser.parse_args(argv)

    verify_signature(os.environ.get(SIGNATURE_SECRET_ENV_VAR), args.issue, args.option, args.signature or None)

    if not issue_still_waiting(args.repo, args.issue):
        raise RuntimeError(
            f"#{args.issue}: задача не в состоянии waiting:owner (закрыта либо метка "
            "снята/сменился раунд) — решение НЕ записано, нажата протухшая кнопка "
            "прошлой эскалации"
        )

    post_issue_comment(args.repo, args.issue, decision_comment(args.option))
    print(f"apply_owner_decision: #{args.issue} — РЕШЕНИЕ: {args.option} записано комментарием")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except RuntimeError as error:
        print(f"::error::apply_owner_decision: {error}", file=sys.stderr)
        sys.exit(1)
