"""Чем провайдер отвергает запрос dsh (#1520).

Вопрос задачи один: почему цепочка провайдеров не даёт ответа, хотя ключи на
месте. Ответ на него — код и ТЕЛО ответа на том же URL, с тем же ключом,
которым ходит прод; всё остальное (какой провайдер «обычно работает»,
какой id «должен быть живым») — правдоподобие, а не замер.

Зонд спрашивает тремя телами, и различие между ними и есть диагноз:
 - чистый Anthropic Messages, `max_tokens: 16` — работает ли МАРШРУТ и КЛЮЧ;
 - он же с манифестным `max_output_tokens` — не упёрлись ли в потолок модели;
 - он же плюс ОДНО поле, которое dsh кладёт сверх протокола — какое именно
   поле провайдер не принимает.

Состав полей сверх протокола снят записью живого исходящего запроса dsh
0.1.7-alpha.2 на локальный `http.server` (не пересказом документации):
заголовки `anthropic-version: 2023-06-01`, `accept: text/event-stream`,
тело `{"model","stream","messages","max_tokens","thinking","system",
"dsh_plugin_packages","dsh_session_log"}`.

Запуск: шаг «Чем провайдер отвергает запрос dsh (#1520)» в
`.github/workflows/quotas.yml` — секреты цепочки есть только там.
"""

from __future__ import annotations

# --- console_utf8 bootstrap (класс: печать кириллицы валит encoding на Windows, issue #723) ---
import importlib.util
from pathlib import Path
_console_utf8_spec = importlib.util.spec_from_file_location(
    "console_utf8", Path(__file__).resolve().parent.parent / "lib" / "console_utf8.py")
_console_utf8_spec.loader.exec_module(importlib.util.module_from_spec(_console_utf8_spec))
# --- конец console_utf8 bootstrap ---

import json
import os
import sys
import urllib.error
import urllib.request

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
PROVIDER_USAGE_MANIFEST = REPO_ROOT / "config" / "provider-usage.json"

#: Сколько печатать от тела ответа. Обрезка не косметическая: тело чужое, и
#: репозиторий публичный — но причина отказа лежит в его начале.
BODY_PREVIEW = 900

#: Версия протокола — та же, что реально шлёт dsh 0.1.7-alpha.2 (замер #1502,
#: запись живого исходящего запроса на локальный сервер). Не «последняя из
#: документации»: спрашиваем ровно то, что спрашивает прод.
ANTHROPIC_VERSION = "2023-06-01"


def messages_api_root(base_url: str) -> str:
    """Дословный перенос правила из `@deepseek-ai/dsh-llm-deepseek/lib/index.js`:

        const base = baseURL.replace(/\\/+$/u, "");
        return new URL(base).pathname.endsWith("/v1") ? base : `${base}/v1`;

    Перенос, а не пересказ: именно неверный пересказ этого правила дал в #1502
    таблицу, неверную для трёх строк из четырёх. Если правило в плагине
    изменится, менять надо ЗДЕСЬ и в том же коммите, где поднимается пин."""
    base = base_url.rstrip("/")
    from urllib.parse import urlparse
    path = urlparse(base).path
    return base if path.endswith("/v1") else f"{base}/v1"


def load_chain(consumer: str = "ai-review",
               manifest_path: Path = PROVIDER_USAGE_MANIFEST) -> list[dict]:
    """Боевая цепочка из манифеста — то же единственное место правды, что
    читает `provider_latency.py::build_manifest_candidates` и
    `dsh_load_provider_chain_from_manifest` в `scripts/lib/dsh-ci.sh`.
    Второй таблицы кандидатов здесь не заводится (AGENTS.md, «Одно место
    правды»)."""
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    chain_name = manifest.get("usage", {}).get(consumer)
    chain = manifest.get("chains", {}).get(chain_name) if chain_name else None
    if not isinstance(chain, list):
        return []
    usable, dropped = [], []
    for entry in chain:
        if not isinstance(entry, dict) or not {"name", "base_url", "secret_env"} <= entry.keys():
            dropped.append(repr(entry)[:80])
            continue
        if not entry_models(entry):
            dropped.append(f"{entry.get('name')}: ни model, ни непустой models")
            continue
        usable.append(entry)
    if dropped:
        # Молча выброшенная запись — silent-wrong (AGENTS.md): читатель отчёта
        # решит, что она отвечает 200, тогда как её просто не спрашивали.
        # Живой случай: первый прогон зонда (run 35996106905) напечатал шесть
        # строк из восьми — NVIDIA-NIM-1/2 задают модели ключом `models`
        # (фолбэк по моделям внутри провайдера), и фильтр по `model` их съел.
        print("::warning::записи цепочки пропущены зондом: " + "; ".join(dropped),
              file=sys.stderr)
    return usable


def entry_models(entry: dict) -> list[str]:
    """Модели записи: `model` (одна) или `models` (фолбэк внутри провайдера).

    Обе формы живые и обе лежат в одном манифесте — `dsh-ci.sh` читает их
    вместе, и зонд обязан спрашивать ровно то же, иначе он меряет не ту
    цепочку, которой ходит прод."""
    single = entry.get("model")
    if isinstance(single, str) and single:
        return [single]
    many = entry.get("models")
    if isinstance(many, list):
        return [m for m in many if isinstance(m, str) and m]
    return []


def redact(text: str, secrets: list[str]) -> str:
    """Маскирование ТОЧНЫХ значений секретов в чужом теле ответа.

    Честная граница: производное (base64, подстрока, часть JWT) это не ловит —
    тот же предел, что у маскирования GitHub (AGENTS.md, «Секреты»). Поэтому
    печатается обрезанное начало тела, а не весь ответ."""
    for value in secrets:
        if value and len(value) >= 8:
            text = text.replace(value, "***")
    return text


#: Поля, которые dsh 0.1.7-alpha.2 кладёт в тело Anthropic Messages-запроса
#: СВЕРХ протокола. Не догадка: снято записью живого исходящего запроса dsh на
#: локальный http.server (см. докстринг модуля) — заголовки
#: `anthropic-version: 2023-06-01`, `accept: text/event-stream`, тело
#: `{"model","stream","messages","max_tokens","thinking","system",
#: "dsh_plugin_packages","dsh_session_log"}`.
#:
#: Значения здесь — минимальные формы тех же ключей: проверяется, отвергает ли
#: провайдер САМ КЛЮЧ, а не его размер (живое `dsh_session_log` весит ~160 КБ,
#: и на нём «отвергли» не отличить от «не приняли такой объём»).
DSH_EXTRA_FIELDS = {
    "thinking": {"type": "disabled"},
    "dsh_plugin_packages": {"version": 1, "packages": []},
    "dsh_session_log": {"version": 1, "sessionFormatVersion": 4},
    "stream": True,
    "system": "Ты ассистент.",
}


#: Объёмы тела для развёртки. Не круглые числа ради красоты: 162 КБ — точный
#: вес запроса dsh на промпт «ping» (замер записью, см. докстринг модуля), то
#: есть НИЖНЯЯ граница живого запроса; в ai-review туда едет ещё и дифф PR.
#: Меньшие и большие значения рядом нужны, чтобы отказ назвал порог, а не
#: только факт («отвергает» без числа не говорит, до чего резать промпт).
BODY_SIZE_SWEEP = (64 * 1024, 162 * 1024, 512 * 1024, 2 * 1024 * 1024)


def padded_body(size_bytes: int) -> dict:
    """Тело заявленного объёма — добиваем тем же полем, которым его добивает
    прод (`dsh_session_log`), а не отдельным «мусорным» ключом: провайдер
    вправе судить по имени поля, и подмена сделала бы замер про другое."""
    return {"dsh_session_log": {"version": 1, "pad": "x" * max(0, size_bytes - 64)}}


def dsh_shaped_body() -> dict:
    """Тело ровно той формы, что снята с живого запроса dsh, целиком.

    Нужна отдельно от полей по одному: отказ может давать не поле, а их
    сочетание (стриминг плюс многоблочный `content`, например). Ни один
    одиночный опыт такого не покажет, и вывод «ни одно поле не отвергнуто»
    без этой строки читался бы как «тело ни при чём»."""
    body = {k: v for k, v in DSH_EXTRA_FIELDS.items()}
    body["messages"] = [{"role": "user", "content": [
        {"type": "text", "text": "ping"},
        {"type": "text", "text": "<system-reminder>\nвторой текстовый блок\n</system-reminder>"},
    ]}]
    return body


def probe(entry: dict, model: str, max_tokens: int = 16,
          extra: dict | None = None, label: str = "протокол",
          timeout: float = 30.0) -> dict:
    """Один провайдер, одна модель, один потолок вывода.

    Возвращает факт, а не вердикт: код и тело. Классификацию делает читатель —
    у скрипта нет данных, чтобы отличить «маршрута нет» от «ключ не тот» лучше,
    чем это сделает сам текст провайдера.

    `max_tokens` вынесен в параметр, потому что он и есть подозреваемый: прод
    шлёт `max_output_tokens` из манифеста (до 131072), зонд по умолчанию — 16.
    Одна и та же запись, отвечающая 200 на 16 и отказом на манифестном
    значении, называет причину точно, а двумя прогонами с разными телами её
    не различить."""
    secret = os.environ.get(entry["secret_env"], "")
    url = f"{messages_api_root(entry['base_url'])}/messages"
    payload = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": "ping"}],
    }
    payload.update(extra or {})
    request = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"), method="POST",
        headers={
            "content-type": "application/json",
            "anthropic-version": ANTHROPIC_VERSION,
            # Прод стримит, и `accept` у него соответствующий. Отправить
            # stream:true с `accept: application/json` — это третья форма,
            # которой не ходит никто, и мерить её бессмысленно.
            **({"accept": "text/event-stream"} if payload.get("stream") else {}),
            "x-api-key": secret,
            "accept": "application/json",
        })
    result = {"name": entry["name"], "url": url, "model": model,
              "max_tokens": max_tokens, "label": label,
              "secret_env": entry["secret_env"], "secret_present": bool(secret)}
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8", "replace")
            result.update(status=response.status, body=body[:BODY_PREVIEW],
                          body_is_answer=classify_answer(response.status, body))
    except urllib.error.HTTPError as error:
        body = error.read().decode("utf-8", "replace")
        result.update(status=error.code, body=body[:BODY_PREVIEW],
                      body_is_answer=classify_answer(error.code, body))
    except OSError as error:
        # «Возможности нет» отделено от «возможность есть, но отказала»
        # (AGENTS.md, fail loud): сеть — не ответ провайдера.
        result.update(status=0, body=f"сеть недоступна: {error}",
                      body_is_answer=False)
    return result


def answered(result: dict) -> bool:
    """См. `classify_answer`. Если вердикт уже снят с ПОЛНОГО тела при запросе
    (`probe` кладёт его в `body_is_answer`), берём его: в отчёт тело попадает
    обрезанным до `BODY_PREVIEW`, и разбирать обрезок как JSON — это тот же
    silent-wrong, только наоборот. Живой случай (прогон 35998173221): запись
    вернула настоящий `"type":"message"`, обрезок не распарсился, и зонд
    напечатал «0 из 24 ответили» при живом ответе модели."""
    if "body_is_answer" in result:
        return bool(result["body_is_answer"])
    return classify_answer(result.get("status"), result.get("body") or "")


def classify_answer(status: int | None, body: str) -> bool:
    """Ответ провайдера — это `{"type":"message"}` в ТЕЛЕ, а не код 200.

    Не педантизм: живой случай (прогон 35996106905) — OpenRouter вернул
    HTTP 200 с телом
    `{"type":"error","error":{"type":"overloaded_error","message":"Upstream
    error from Nvidia: Service temporarily overloaded"}}`. Зонд, считающий
    ответом код, назвал бы такую запись рабочей — и следующий читатель искал
    бы поломку где угодно, кроме места, где она есть (AGENTS.md: «проверяй
    видимый результат, а не шаг»; «HTTP 200» прямо назван не доказательством).

    Стриминговый ответ (`accept: text/event-stream`) приходит кадрами SSE —
    там признак тот же, но внутри строк `data:`."""
    if status != 200:
        return False
    for chunk in ([body] if not body.startswith("event:") and not body.startswith("data:")
                  else [line[5:].strip() for line in body.splitlines()
                        if line.startswith("data:")]):
        try:
            parsed = json.loads(chunk)
        except ValueError:
            continue
        if isinstance(parsed, dict):
            if parsed.get("type") == "error":
                return False
            if parsed.get("type") in ("message", "message_start"):
                return True
    return False


def format_report(results: list[dict]) -> str:
    # Имя переменной секрета — в таблице, значение — нигде. Читателю отчёта
    # чинить конфигурацию, и «секрета нет» без имени переменной не говорит,
    # ЧТО именно положить (AGENTS.md: утверждение обязано нести адрес).
    lines = ["запись | тело | max_tokens | HTTP | секрет | переменная | URL",
             "---|---|---|---|---|---|---"]
    for r in results:
        lines.append(f"{r['name']} | {r.get('label', 'протокол')} | {r['max_tokens']} | "
                     f"{r['status']}{'' if answered(r) else ' (тело НЕ ответ)'} | "
                     f"{'есть' if r['secret_present'] else 'НЕТ'} | "
                     f"`{r['secret_env']}` | `{r['url']}`")
    lines.append("")
    lines.append("Ответы провайдеров дословно (обрезаны, секреты замаскированы):")
    for r in results:
        lines.append("")
        lines.append(f"── {r['name']} (HTTP {r['status']}, модель `{r['model']}`, "
                     f"max_tokens={r['max_tokens']}, тело: {r.get('label', 'протокол')})")
        lines.append(r["body"] or "(пустое тело)")
    return "\n".join(lines)


def main() -> int:
    chain = load_chain()
    if not chain:
        print("::error::боевая цепочка не прочитана из config/provider-usage.json — "
              "замер невозможен; это «возможности нет», а не «провайдеры молчат»",
              file=sys.stderr)
        return 1
    secrets = [os.environ.get(e["secret_env"], "") for e in chain]
    results = []
    for entry in chain:
        # Манифестный потолок — ровно то, что подставляет прод; 16 — заведомо
        # безобидный минимум. Разница между двумя ответами и есть ответ на
        # вопрос задачи: «маршрут не тот» или «тело запроса не то».
        prod_cap = entry.get("max_output_tokens")
        caps = [16] if not isinstance(prod_cap, int) else [16, prod_cap]
        for model in entry_models(entry):
            for cap in caps:
                r = probe(entry, model, max_tokens=cap)
                r["body"] = redact(r["body"], secrets)
                results.append(r)
            # Поля сверх протокола — по ОДНОМУ. Скопом «отвергли» не называет,
            # что именно отвергли, а чинить надо конкретное поле; разбор по
            # одному даёт адрес (AGENTS.md: алерт не гадает).
            if not answered(results[-1]):
                continue
            for field, value in DSH_EXTRA_FIELDS.items():
                r = probe(entry, model, max_tokens=caps[-1],
                          extra={field: value}, label=f"+{field}")
                r["body"] = redact(r["body"], secrets)
                results.append(r)
            r = probe(entry, model, max_tokens=caps[-1],
                      extra=dsh_shaped_body(), label="форма dsh целиком")
            r["body"] = redact(r["body"], secrets)
            results.append(r)
            for size in BODY_SIZE_SWEEP:
                r = probe(entry, model, max_tokens=caps[-1],
                          extra=padded_body(size), label=f"объём {size // 1024} КБ")
                r["body"] = redact(r["body"], secrets)
                results.append(r)
                if not answered(r):
                    # Дальше по развёртке смысла нет: порог найден, а каждый
                    # следующий вызов — реальный расход чужой квоты.
                    break
    print(format_report(results))
    ok = [r for r in results if answered(r)]
    code_200 = [r for r in results if r["status"] == 200]
    print("")
    print(f"Ответили моделью на Anthropic-маршруте: {len(ok)} из {len(results)}")
    if len(code_200) != len(ok):
        print(f"Из них отдали код 200 с телом-ошибкой: {len(code_200) - len(ok)} — "
              "код 200 у этих провайдеров не означает ответа, и считать по нему "
              "нельзя (живой случай: прогон 35996106905, overloaded_error в 200)")
    for r in results:
        if (r.get("label", "") == "форма dsh целиком" and not answered(r)
                and all(answered(o) for o in results
                        if o["name"] == r["name"] and o["model"] == r["model"]
                        and o.get("label") != "форма dsh целиком")):
            print(f"СОЧЕТАНИЕ: {r['name']}/{r['model']} — каждое поле по "
                  f"отдельности принято (200), а снятая с dsh форма целиком "
                  f"даёт {r['status']}: отвергает не одно поле, а их сочетание.")
    for r in results:
        if r.get("label", "").startswith("объём ") and not answered(r):
            ok = [o for o in results if o["name"] == r["name"]
                  and o.get("label", "").startswith("объём ")
                  and answered(o)]
            last_ok = ok[-1]["label"] if ok else "ни одного"
            print(f"ПОРОГ ОБЪЁМА: {r['name']}/{r['model']} — принят {last_ok}, "
                  f"отвергнут «{r['label']}» с кодом {r['status']}. Форма тела "
                  f"та же, различается только размер.")
    for r in results:
        if r.get("label", "").startswith("+") and not answered(r):
            print(f"ПОЛЕ ОТВЕРГНУТО: {r['name']}/{r['model']} — та же запись "
                  f"отвечает 200 на чистом протоколе, а с полем "
                  f"`{r['label'][1:]}` даёт {r['status']}. Это поле шлёт dsh "
                  f"сверх Anthropic Messages, и отказ приходит из-за него.")
    for r in results:
        if r["max_tokens"] == 16 and answered(r) and not r.get("label", "").startswith("+"):
            prod = [o for o in results
                    if o["name"] == r["name"] and o["model"] == r["model"]
                    and o["max_tokens"] != 16
                    and not o.get("label", "").startswith("+")]
            for o in prod:
                if not answered(o):
                    print(f"РАЗЛИЧИЕ: {r['name']}/{r['model']} — маршрут исправен "
                          f"(200 при max_tokens=16), отказ {o['status']} приходит "
                          f"на манифестном max_tokens={o['max_tokens']}: чинить "
                          f"надо потолок в манифесте, а не base_url")
    if not ok:
        print("Ни одна запись цепочки не обслуживает форму, которой ходит dsh — "
              "это факт замера, а не вердикт о причине: текст каждого отказа выше.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
