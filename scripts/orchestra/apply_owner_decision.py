#!/usr/bin/env python3
"""Применение решения владельца, присланного нажатием инлайн-кнопки в Telegram
(#254). Запускается workflow'ом .github/workflows/owner-decision.yml по
repository_dispatch (event_type owner-decision), который морда шлёт узким
GH_DISPATCH_TOKEN (ADR 0008, Contents+Actions, без Issues). Этот job читает
ТРИ секрета репозитория — TELEGRAM_WEBHOOK_SECRET (проверка подписи ниже;
до правки #1251 секретов не читал вовсе, прежняя формулировка этого
докстринга «никакого нового секрета» описывала то прежнее поведение и
противоречила новому тексту ниже — модуль противоречил сам себе,
исправлено ревью PR #1254) плюс TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID
(ответ владельцу на ОТВЕРГНУТОЕ нажатие, #1398 — раздел «Отказ не пропадает
молча» ниже), — плюс issues:write из permissions workflow'а (github.token).
Новых секретов не заведено: все три уже читают соседние job'ы конвейера.

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
ТРИ РАЗНЫХ отказа, ни один не пишет комментарий РЕШЕНИЯ (след отказа —
другой текст и другой класс, см. «Отказ не пропадает молча» ниже; что он
не может нести маркер решения, проверяет `assert_no_decision_marker` тем
же регулярным выражением, которое читает гвардия) (класс #1096, носитель
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
окна нажатие кнопки красит job (SignatureMissing): постоянного «мягкого»
режима, принимающего пустую подпись бессрочно, здесь нет и не будет —
именно бессрочный обход и был дырой issue #1251.

Прежняя редакция этого абзаца называла такой сбой «самостоятельно
устраняющимся» и «видимым» — оба слова оказались неверны, и цену назвал
живой случай #1379 (разбор — #1398). Видимым он не был: единственным следом
был красный прогон `owner-decision.yml`, у которого нет ни одного читателя,
и владелец, нажавший кнопку, не узнал НИЧЕГО. Самоустраняющимся — только
если пустая подпись пришла от воркера в окне деплоя; если секрета в воркере
просто нет, не устранится никогда, а отсюда эти два случая неразличимы.
Поэтому текст отказа больше не выбирает между ними (AGENTS.md, «алерт не
гадает»), а уведомление о нём — обязательный шаг, см. ниже.

Отказ не пропадает молча (issue #1398). Любой отказ — `DecisionRefused` с
машинным кодом причины и с ГАЗОМ (что именно возвращает движение; отказ без
газа запрещён AGENTS.md, а до #1398 весь этот путь был ровно таким). После
отказа `__main__` зовёт `notify_refusal`, и у отказа появляются два
адресата: владелец — сообщением в Telegram тем же каналом, которым пришёл
вопрос; следующий агент — комментарием в самой задаче (след дедуплицируется
по паре причина+вариант, класс #1100). Job при этом остаётся КРАСНЫМ:
уведомление добавлено к громкому отказу, а не вместо него. Доставка
best-effort по каждому каналу, но её провал называется вслух — «сигнал
ушёл» и «сигнал не ушёл» лечатся по-разному.

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
владельцу «Принято» (answerCallbackQuery по 204 от dispatch), поэтому отказ
громкий (RuntimeError → `::error::` → exit 1), не тихий no-op. Утверждение
прежней редакции «красный job здесь единственный видимый сигнал расхождения»
верным больше НЕ является и снято: с #1398 отказ громкий трижды — красный
job, сообщение владельцу в Telegram и след с причиной в самой задаче (см.
«Отказ не пропадает молча» ниже). Тот же приём, что абзацем выше про
«самоустраняющийся» сбой: устаревшее утверждение правится, а не оставляется
рядом с новым — два ответа на один вопрос в одном докстринге дороже любого
из них.

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

from pulse_guard import (DECISION_COMMENT_PREFIX, all_issue_comments, gh, post_issue_comment,
                         send_telegram)
from waiting_owner_guard import DECISION_MARKER_RE, WAITING_OWNER_LABEL

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


class DecisionRefused(RuntimeError):
    """Отказ применить нажатие — с МАШИННЫМ кодом причины и с газом (#1398).

    RuntimeError остаётся базой сознательно: `__main__` и уже написанные тесты
    ловят её, поведение существующих вызовов не меняется. Новое — два поля:

    - `reason` — код причины, по которому дедуплицируется след в задаче
      (класс #1100: повторное нажатие той же кнопки не плодит копии);
    - `gas` — что именно возвращает движение. Отказ без газа запрещён
      правилами репозитория (AGENTS.md, «Тормоз без газа не принимается»), а
      до #1398 весь этот путь был ровно таким: нажатие отвергнуто, и дальше
      не происходило НИЧЕГО.
    """

    def __init__(self, reason: str, message: str, gas: str) -> None:
        super().__init__(message)
        self.reason = reason
        self.gas = gas
        # Контекст нажатия навешивает `main` (`with_context`), а читает его
        # `__main__` при уведомлении. Через исключение, а не через env: репо,
        # задача и вариант уже пришли аргументами, и вторая их копия в
        # переменных окружения была бы вторым местом правды — расходится
        # молча, лечится дважды.
        self.repo: str | None = None
        self.issue_number: int | None = None
        self.option: int | None = None

    def with_context(self, repo: str, issue_number: int, option: int) -> "DecisionRefused":
        self.repo, self.issue_number, self.option = repo, issue_number, option
        return self


def verify_signature(secret: str | None, issue_number: int, option: int, signature: str | None) -> None:
    """Три РАЗНЫХ отказа (issue #1251, класс #1096 — не схлопывать причины в
    одно НЕТ), ни один не должен привести к post_issue_comment: секрет не
    настроен в этом job'е, подписи нет в client_payload, подпись не
    совпала (включая не-ASCII подделку — она mismatch, не TypeError,
    сравнение байтовое). См. докстринг модуля, раздел «Подпись
    client_payload», за объяснением каждой причины и переходного режима."""
    if not secret:
        raise DecisionRefused(
            "github_secret_missing",
            f"секрет {SIGNATURE_SECRET_ENV_VAR} не настроен в этом job'е (GitHub Actions "
            "repository secret) — подпись НЕЧЕМ проверить, решение НЕ записано",
            f"добавить {SIGNATURE_SECRET_ENV_VAR} в GitHub Actions repository secrets и "
            "нажать кнопку ещё раз; пока секрета нет, кнопочный путь не работает вовсе",
        )
    if not signature:
        raise DecisionRefused(
            "signature_missing",
            # Факт, и только факт. Прежняя редакция называла здесь две гипотезы
            # («переходное окно деплоя, самоустраняется» ЛИБО прямой вызов) —
            # это гадание, запрещённое AGENTS.md («алерт не гадает»), и одна из
            # гипотез успокаивающая: если секрета в воркере просто нет,
            # «самоустранится» не произойдёт никогда, а читатель уже успокоен.
            "client_payload пришёл с пустой подписью (signature пусто) — решение НЕ "
            "записано. Причину отсюда установить НЕЛЬЗЯ: этот job не читает ни "
            "секреты Cloudflare, ни версию задеплоенного воркера",
            "установить причину одним из двух: `wrangler secret list` у воркера "
            f"(есть ли {SIGNATURE_SECRET_ENV_VAR}) и версия воркера против даты правки "
            "#1251; пока причина не устранена, решение применяется комментарием-маркером "
            "в самой задаче (PROTOCOL.md, «Решение владельца как артефакт»)",
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
        raise DecisionRefused(
            "signature_mismatch",
            f"#{issue_number}: подпись client_payload не совпадает с ожидаемой для "
            f"варианта {option} — подделка, испорченный payload или подпись от другой "
            "пары issue/option; решение НЕ записано",
            "если кнопку нажимал владелец — повторить нажатие на СВЕЖЕМ сообщении "
            "эскалации (подпись привязана к паре задача+вариант); если нет — этот "
            "отказ и есть штатная работа заслона #1251, делать ничего не нужно",
        )


# Маркер следа отказа в задаче (#1398). Форма — скобочная строка, как
# WAITING_OWNER_ESCALATE_MARKER и прочие маркеры гвардий: в прозе так не пишут,
# а машина находит подстрокой. Дедуп по ПАРЕ причина+вариант: повторное нажатие
# той же кнопки при той же неисправности не плодит копии следа (класс #1100 —
# 114 дублей одного маркера), а нажатие ДРУГОГО варианта или отказ по ДРУГОЙ
# причине — другое событие, и след у него свой.
REFUSAL_MARKER_PREFIX = "[нажатие владельца отвергнуто: "


def refusal_marker(reason: str, issue_number: int, option: int) -> str:
    return f"{REFUSAL_MARKER_PREFIX}{reason}:{issue_number}:{option}]"


def refusal_comment(refusal: "DecisionRefused", issue_number: int, option: int) -> str:
    """След отказа в самой задаче — чтобы причина осталась для СЛЕДУЮЩЕГО
    агента, а не только в чужом чате и в логе прогона, которого у этого
    события нет читателя (#1398).

    Текст обязан НЕ нести маркер решения ни одной строкой: комментарий,
    начинающийся с «<маркер>: N», снял бы `waiting:owner` и записал бы то
    самое решение, в применении которого мы сейчас отказываем — silent-wrong
    ровно наоборот. Проверяется не глазами, а `assert_no_decision_marker`."""
    return (
        f"{refusal_marker(refusal.reason, issue_number, option)}\n"
        f"⛔ Нажатие кнопки по этой задаче (вариант {option}) НЕ применено.\n\n"
        f"**Причина** ({refusal.reason}): {refusal}\n\n"
        f"**Что возвращает движение:** {refusal.gas}\n\n"
        f"{_label_state_line(refusal.reason)} "
        "Разбор класса «нажатие пропадает молча» — #1398."
    )


def _label_state_line(reason: str) -> str:
    """Состояние метки называется только там, где оно ПРОВЕРЕНО (блокирующая
    находка ai-ревью PR #1401, второй круг). `not_waiting` — единственная
    причина, которая отказывает ПОСЛЕ обращения к задаче: метки как раз НЕТ,
    это проверенный факт и одновременно причина отказа. Три причины подписи
    (`github_secret_missing`/`signature_missing`/`signature_mismatch`)
    отказывают ДО обращения к API за состоянием — контракт держит тест
    `test_main_stops_before_issue_still_waiting_check_on_bad_signature`, — а
    случай реален: решение уже записано комментарием «РЕШЕНИЕ: N», гвардия на
    пульсе сняла метку, владелец нажал кнопку — отказ по подписи приходит на
    задачу УЖЕ БЕЗ метки, и «задача остаётся с меткой» было бы утверждением
    без проверки, тем же классом, который для `not_waiting` чинит
    `test_refusal_comment_does_not_claim_a_label_that_is_gone`. Поэтому здесь
    job называет ровно то, что знает: ЭТУ метку ЭТОТ отказ не трогал, а
    актуальное состояние — в метках задачи."""
    if reason == "not_waiting":
        return (f"Метки `{WAITING_OWNER_LABEL}` на задаче нет (снята или задача закрыта) — "
                "именно поэтому нажатие и отвергнуто; решение по нему не записано.")
    return (f"Этот отказ случился до проверки состояния задачи: метку `{WAITING_OWNER_LABEL}` "
            "он не снимал и не ставил, актуальное состояние видно в метках задачи; "
            "решение по нему не записано.")


def refusal_telegram_text(repo: str, issue_number: int, option: int,
                          refusal: "DecisionRefused") -> str:
    """Ответ ТОМУ, кто нажал, тем же каналом, которым пришёл вопрос. Без него
    владелец видит нажатую кнопку и считает решение принятым: «применено» и
    «выброшено» он не различает ничем (живой случай #1379 — сутки простоя)."""
    return (
        f"⛔ Нажатие по #{issue_number} (вариант {option}) не применено.\n\n"
        f"Причина: {refusal}\n\n"
        f"Что делать: {refusal.gas}\n\n"
        f"https://github.com/{repo}/issues/{issue_number}"
    )


def assert_no_decision_marker(text: str) -> None:
    """Страховка против худшего исхода этого файла: текст ОТКАЗА, случайно
    несущий маркер решения, применил бы решение, в котором отказано —
    `waiting_owner_guard` читает маркер в любом комментарии от кого угодно и
    автора не смотрит вовсе (AGENTS.md, #1251). Поэтому проверка машинная тем
    же регулярным выражением, что читает гвардия, а не «я посмотрел глазами»."""
    if DECISION_MARKER_RE.search(text):
        raise RuntimeError(
            "текст отказа несёт маркер решения — публикация применила бы решение, "
            "в котором отказано; комментарий НЕ отправлен"
        )


def refusal_already_reported(repo: str, issue_number: int, marker: str) -> bool:
    """True — след с этим маркером в задаче уже есть (то же нажатие при той же
    неисправности повторили). Ошибка чтения комментариев НЕ выдаётся за «следа
    нет»: она пробрасывается наверх, иначе дедуп молча выключался бы при любом
    сбое API и плодил копии — тот же класс, что «дедуп issue-create молча
    выключается без repo» (#1395).

    Листание ВСЕЙ истории, а не первой страницы (класс #308/#276, гвардия
    `scripts/lib/test_pagination_guard.py`): эндпоинт отдаёт комментарии
    СТАРЕЙШИМИ вперёд и `sort`/`direction` молча игнорирует, поэтому на
    задаче длиннее ста комментариев одна страница не содержит свежий маркер
    вовсе — дедуп перестал бы находить собственный след и плодил бы копии
    при каждом повторном нажатии, ровно класс #1100, который этот файл и
    называет своей мотивировкой. Обходчик публичный и уже импортируется
    отсюда, своего цикла не завожу (находка ai-ревью PR #1401)."""
    comments = all_issue_comments(repo, issue_number) or []
    return any(marker in (comment.get("body") or "") for comment in comments)


def notify_refusal(repo: str, issue_number: int, option: int,
                   refusal: "DecisionRefused") -> None:
    """Два адресата отказа, и ни один не обязателен для второго (#1398).

    Доставка best-effort ПО КАЖДОМУ каналу, но провал канала называется
    вслух: «сигнал ушёл» и «сигнал не ушёл» лечатся по-разному, а job и так
    красный — глушить здесь нечего. Сам отказ применить решение от результата
    доставки не зависит: он уже принят выше по стеку."""
    marker = refusal_marker(refusal.reason, issue_number, option)
    comment = refusal_comment(refusal, issue_number, option)
    assert_no_decision_marker(comment)

    try:
        if refusal_already_reported(repo, issue_number, marker):
            print(f"apply_owner_decision: след отказа {refusal.reason} по #{issue_number} "
                  f"(вариант {option}) в задаче уже есть — второй не пишу")
        else:
            post_issue_comment(repo, issue_number, comment)
            print(f"apply_owner_decision: причина отказа записана комментарием в #{issue_number}")
    except (RuntimeError, OSError) as error:
        print(f"::warning::apply_owner_decision: след отказа в #{issue_number} НЕ записан "
              f"({error}) — причина осталась только в этом логе", file=sys.stderr)

    if send_telegram(refusal_telegram_text(repo, issue_number, option, refusal)):
        print("apply_owner_decision: владельцу отправлено уведомление об отказе")
    else:
        print("::warning::apply_owner_decision: уведомление владельцу НЕ отправлено — "
              "нажатие осталось без ответа в том канале, где было сделано", file=sys.stderr)


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

    # Отказ несёт контекст нажатия, но НЕ уведомляет сам: `main` остаётся
    # чистым решением («записать или отказать»), и его контракт «при отказе не
    # написано ни строчки решения» проверяется тестами без сети. Уведомление —
    # шаг `__main__`, у него и адресаты, и best-effort доставка.
    try:
        verify_signature(os.environ.get(SIGNATURE_SECRET_ENV_VAR), args.issue, args.option,
                         args.signature or None)
        _check_issue_still_waiting(args.repo, args.issue)
    except DecisionRefused as refusal:
        raise refusal.with_context(args.repo, args.issue, args.option) from None

    post_issue_comment(args.repo, args.issue, decision_comment(args.option))
    print(f"apply_owner_decision: #{args.issue} — решение (вариант {args.option}) записано комментарием")
    return 0


def _check_issue_still_waiting(repo: str, issue_number: int) -> None:
    if not issue_still_waiting(repo, issue_number):
        raise DecisionRefused(
            "not_waiting",
            f"#{issue_number}: задача не в состоянии waiting:owner (закрыта либо метка "
            "снята/сменился раунд) — решение НЕ записано, нажата протухшая кнопка "
            "прошлой эскалации",
            "решение по этой задаче уже применено или задача закрыта — делать ничего "
            "не нужно; клавиатуры прошлых эскалаций остаются нажимаемыми, нажимать "
            "нужно кнопки САМОГО СВЕЖЕГО сообщения",
        )


if __name__ == "__main__":
    try:
        sys.exit(main())
    except DecisionRefused as refusal:
        # Отказ остаётся ГРОМКИМ (::error:: + exit 1) — job красный, как и был.
        # Новое — у отказа появились адресаты: до #1398 красный прогон был
        # ЕДИНСТВЕННЫМ следом, а читателя у этого события нет ни одного.
        print(f"::error::apply_owner_decision: {refusal}", file=sys.stderr)
        print(f"::error::apply_owner_decision: газ — {refusal.gas}", file=sys.stderr)
        if refusal.repo and refusal.issue_number is not None and refusal.option is not None:
            notify_refusal(refusal.repo, refusal.issue_number, refusal.option, refusal)
        else:
            print("::warning::apply_owner_decision: у отказа нет контекста нажатия — "
                  "уведомить некого, причина осталась только в этом логе", file=sys.stderr)
        sys.exit(1)
    except RuntimeError as error:
        print(f"::error::apply_owner_decision: {error}", file=sys.stderr)
        sys.exit(1)