#!/usr/bin/env python3
"""Оркестратор сообщений: gather (доверенный) -> DSH headless -> verdict (доверенный).

Аналог ai_review.py, но для произвольных chat-сообщений владельца вместо диффа PR.
Читает сообщение из DO, собирает контекст из GitHub (issues/PR/labels),
формирует промпт, DSH рассуждает, verdict пишет результат в DO через CAS
и отправляет ответ в канал происхождения (Telegram chat_id).
"""

# --- console_utf8 bootstrap (класс: печать кириллицы валит encoding на Windows, issue #723) ---
import importlib.util
from pathlib import Path
_console_utf8_spec = importlib.util.spec_from_file_location(
    "console_utf8", Path(__file__).resolve().parent.parent / "lib" / "console_utf8.py")
_console_utf8_spec.loader.exec_module(importlib.util.module_from_spec(_console_utf8_spec))
# --- конец console_utf8 bootstrap ---

import argparse
import importlib.util
import json
import os
import sys
import textwrap
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent.parent

# Загрузка общих модулей
_LIBS = {
    "review_labels": SCRIPT_DIR.parent / "lib" / "review_labels.py",
    "task_ref": SCRIPT_DIR.parent / "lib" / "task_ref.py",
    "pool_issue": SCRIPT_DIR.parent / "lib" / "pool_issue.py",
}
_modules = {}
for name, path in _LIBS.items():
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    _modules[name] = module

review_labels = _modules["review_labels"]
task_ref = _modules["task_ref"]
pool_issue = _modules["pool_issue"]


def gh(*args: str) -> str:
    """Вызов gh CLI с JSON-выводом. Падает громко при ошибке."""
    result = subprocess.run(["gh", *args], capture_output=True, text=True, encoding="utf-8")
    if result.returncode != 0:
        raise RuntimeError(f"gh {' '.join(args)}: {result.stderr.strip()}")
    return result.stdout.strip()


def tg_html(text: str) -> str:
    """Экранирование текста для Telegram HTML parse_mode."""
    return (text.replace("&", "&").replace("<", "<").replace(">", ">")
            .replace('"', """).replace("'", "'"))


def send_telegram(text: str, bot_token: str, chat_id: str, as_html: bool = False) -> bool:
    """Отправка сообщения в Telegram. Возвращает True/False, не бросает."""
    if not bot_token or not chat_id:
        print("::warning::TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID не заданы — сигнал не отправлен",
              file=sys.stderr)
        return False
    payload_text = text if as_html else tg_html(text)
    import subprocess
    args = ["curl", "-fsS", "--max-time", "30", "-X", "POST",
            f"https://api.telegram.org/bot{bot_token}/sendMessage",
            "--data-urlencode", f"chat_id={chat_id}",
            "--data-urlencode", "parse_mode=HTML",
            "--data-urlencode", f"text={payload_text}"]
    try:
        result = subprocess.run(args, capture_output=True, text=True, encoding="utf-8")
    except OSError as error:
        print(f"::warning::curl недоступен, сигнал не отправлен: {error}", file=sys.stderr)
        return False
    if result.returncode != 0:
        print(f"::warning::Telegram не принял сигнал: {result.stderr.strip()}", file=sys.stderr)
        return False
    return True


def fetch_open_issues_context() -> str:
    """Собирает контекст открытых задач и PR для промпта оркестратора."""
    try:
        # Открытые задачи с меткой task
        issues_json = gh("api", "repos/mytab0r/edge-harness/issues",
                         "--jq", ".[] | select(.state==\"open\" and (.labels | map(.name) | index(\"task\"))) | {number: .number, title: .title, labels: .labels | map(.name), assignees: .assignees | map(.login)}")
        issues = []
        for line in issues_json.strip().split("\n"):
            if line.strip():
                issues.append(json.loads(line))
        
        # Открытые PR
        prs_json = gh("api", "repos/mytab0r/edge-harness/pulls",
                      "--jq", ".[] | select(.state==\"open\") | {number: .number, title: .title, head: .head.ref, labels: .labels | map(.name)}")
        prs = []
        for line in prs_json.strip().split("\n"):
            if line.strip():
                prs.append(json.loads(line))
        
        lines = ["## Контекст проекта (GitHub)"]
        if issues:
            lines.append("### Задачи (пул)")
            for issue in issues[:20]:  # лимит для промпта
                assignees = ", ".join(issue.get("assignees", [])) or "нет"
                labels = ", ".join(issue.get("labels", []))
                lines.append(f"- #{issue['number']}: {issue['title']} [{labels}] (assignee: {assignees})")
        else:
            lines.append("### Задачи (пул): пусто")
        
        if prs:
            lines.append("### Открытые PR")
            for pr in prs[:10]:
                labels = ", ".join(pr.get("labels", []))
                lines.append(f"- PR #{pr['number']}: {pr['title']} (ветка: {pr['head']}) [{labels}]")
        else:
            lines.append("### Открытые PR: пусто")
        
        return "\n".join(lines)
    except Exception as e:
        return f"## Контекст проекта (GitHub)\nОшибка сбора: {e}"


def cmd_gather(args: argparse.Namespace) -> int:
    """Сбор фактов и формирование промпта для DSH."""
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    
    message_id = args.message_id
    text = args.text
    chat_id = args.chat_id
    source = args.source
    
    # Собираем контекст проекта
    context = fetch_open_issues_context()
    
    # Формируем промпт
    prompt = f"""Ты — оркестратор проекта edge-harness. Прочитал сообщение владельца из канала {source} (chat_id: {chat_id}) и должен решить, что сделать.

{context}

---

## Сообщение владельца (message_id: {message_id})

{text}

---

## Твоя задача

Проанализируй сообщение и реши ОДНО из действий:
1. **ЗАВЕСТИ ЗАДАЧУ** — если сообщение содержит запрос на работу, который нужно оформить в пул задач.
2. **ОТВЕТИТЬ** — если это вопрос, требующий развёрнутого ответа (технический, архитектурный, исследовательский).
3. **ПОПРАВИТЬ ДОКУМЕНТ** — если сообщение указывает на ошибку/устаревание в документации.
4. **НИЧЕГО НЕ ДЕЛАТЬ** — если сообщение не требует действия (информация, подтверждение, шум).

Правила:
- Не дублируй существующие задачи — проверь пул выше по смыслу, не по строке.
- Ответ должен быть конкретным и полезным, не общим.
- Если заводишь задачу — сформулируй заголовок и критерий готовности.
- Если отвечаешь — дай полный ответ, опираясь на контекст проекта.
- Если поправляешь документ — назови файл и что именно поправить.

## Формат ответа

Ответь ТЕКСТОМ (не JSON). В конце ОБЯЗАТЕЛЬНО добавь машиночитаемую строку результата:

РЕЗУЛЬТАТ: {{"action": "create_task|answer|edit_doc|none", "title": "...", "details": "...", "file": "...", "answer_text": "..."}}

Где:
- action: одно из create_task|answer|edit_doc|none
- title: заголовок задачи (для create_task) или краткая суть (для остальных)
- details: детали задачи/ответа/правки (многострочный, экранирован в JSON)
- file: путь к файлу (для edit_doc)
- answer_text: текст ответа владельцу (для action=answer), для остальных может быть пустым

Примеры:
- РЕЗУЛЬТАТ: {{"action": "create_task", "title": "Добавить тест для X", "details": "Нужно покрыть функцию Y тестами...", "answer_text": "Завёл задачу #N: добавить тест для X"}}
- РЕЗУЛЬТАТ: {{"action": "answer", "title": "Ответ на вопрос про Y", "details": "", "answer_text": "Y работает так: ..."}}
- РЕЗУЛЬТАТ: {{"action": "edit_doc", "title": "Правка docs/INDEX.md", "details": "В разделе Z устарела ссылка...", "file": "docs/INDEX.md", "answer_text": "Поправил docs/INDEX.md: обновил ссылку на Z"}}
- РЕЗУЛЬТАТ: {{"action": "none", "title": "Информационное сообщение", "details": "Владелец подтвердил, что X работает", "answer_text": "Принято, спасибо"}}
"""
    
    # Записываем файлы для DSH
    (out_dir / "prompt.txt").write_text(prompt, encoding="utf-8")
    (out_dir / "meta.json").write_text(json.dumps({
        "message_id": message_id,
        "chat_id": chat_id,
        "source": source,
        "text": text,
        "context": context,
    }, ensure_ascii=False), encoding="utf-8")
    
    # Пустой дифф не бывает — всегда есть сообщение
    (out_dir / "failure_reason.txt").write_text("", encoding="utf-8")
    
    print(f"prompt written to {out_dir}/prompt.txt")
    return 0


def parse_result(text: str) -> dict | None:
    """Парсит строку РЕЗУЛЬТАТ: из ответа модели."""
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("РЕЗУЛЬТАТ:"):
            json_part = line[len("РЕЗУЛЬТАТ:"):].strip()
            try:
                return json.loads(json_part)
            except json.JSONDecodeError as e:
                print(f"::error::не смог разобрать РЕЗУЛЬТАТ: {e}: {json_part}", file=sys.stderr)
                return None
    return None


def create_github_issue(title: str, body: str) -> tuple[int, str]:
    """Создаёт issue в GitHub и возвращает (number, url)."""
    created = gh("api", "-X", "POST", "repos/mytab0r/edge-harness/issues",
                 "-f", f"title={title}",
                 "-f", f"body={body}",
                 "-f", "labels[]=task",
                 "-f", "labels[]=source:orchestrator")
    data = json.loads(created)
    return data["number"], data["html_url"]


def edit_document(file_path: str, details: str) -> str:
    """Создаёт задачу на правку документа (прямого редактирования нет — это отдельный PR)."""
    # Оркестратор не редактирует файлы напрямую — заводит задачу на правку.
    # Возвращаем сообщение для владельца.
    return f"Завёл задачу на правку {file_path}: {details}"


def cmd_verdict(args: argparse.Namespace) -> int:
    """Обработка ответа модели: запись в DO через CAS, отправка ответа в канал."""
    message_id = args.message_id
    chat_id = args.chat_id
    claimed_ts = int(args.claimed_ts)
    source = args.source
    answer_file = Path(args.answer)
    harness_url = args.harness_url
    hands_token = args.hands_token
    telegram_bot_token = args.telegram_bot_token
    telegram_chat_id = args.telegram_chat_id
    
    if not answer_file.exists():
        print(f"::error::файл ответа {answer_file} не найден", file=sys.stderr)
        return 1
    
    answer_text = answer_file.read_text(encoding="utf-8").strip()
    result = parse_result(answer_text)
    
    if not result:
        print("::error::РЕЗУЛЬТАТ не найден или невалиден в ответе модели", file=sys.stderr)
        # Записываем failed в DO
        import requests
        try:
            requests.post(
                f"{harness_url}/api/messages/{message_id}/finish",
                headers={"Authorization": f"Bearer {hands_token}", "Content-Type": "application/json"},
                json={"status": "failed", "result": {"error": "model returned invalid result"}, "claimed_ts": claimed_ts},
                timeout=30
            )
        except Exception as e:
            print(f"::warning::не смог записать failed в DO: {e}", file=sys.stderr)
        return 1
    
    action = result.get("action", "none")
    title = result.get("title", "")
    details = result.get("details", "")
    file_path = result.get("file", "")
    answer_to_owner = result.get("answer_text", "")
    
    # Выполняем действие
    final_result = {"action": action, "title": title}
    response_text = answer_to_owner
    
    try:
        if action == "create_task":
            # Формируем тело задачи
            task_body = f"""## Сообщение владельца (orchestrator message #{message_id})

{details}

---
*Создано автоматически оркестратором из {source}.*"""
            issue_number, issue_url = create_github_issue(title, task_body)
            final_result["issue_number"] = issue_number
            final_result["issue_url"] = issue_url
            if not answer_to_owner:
                response_text = f"Завёл задачу #{issue_number}: {title}"
        
        elif action == "edit_doc":
            # Прямого редактирования нет — создаём задачу на правку
            task_title = f"Правка документа: {file_path}"
            task_body = f"""## Правка документа (orchestrator message #{message_id})

**Файл:** {file_path}
**Что поправить:** {details}

---
*Создано автоматически оркестратором из {source}.*"""
            issue_number, issue_url = create_github_issue(task_title, task_body)
            final_result["issue_number"] = issue_number
            final_result["issue_url"] = issue_url
            final_result["file"] = file_path
            if not answer_to_owner:
                response_text = f"Завёл задачу #{issue_number} на правку {file_path}"
        
        elif action == "answer":
            final_result["answer_text"] = answer_to_owner
            if not answer_to_owner:
                response_text = "Оркестратор не сформулировал ответ"
        
        elif action == "none":
            if not answer_to_owner:
                response_text = "Принято, действий не требуется"
        
        else:
            raise ValueError(f"неизвестное action: {action}")
    
    except Exception as e:
        print(f"::error::действие {action} не удалось: {e}", file=sys.stderr)
        final_result = {"action": "failed", "error": str(e)}
        response_text = f"Ошибка при выполнении действия: {e}"
    
    # Записываем результат в DO через CAS (finish endpoint)
    import requests
    try:
        finish_resp = requests.post(
            f"{harness_url}/api/messages/{message_id}/finish",
            headers={"Authorization": f"Bearer {hands_token}", "Content-Type": "application/json"},
            json={"status": "done", "result": final_result, "claimed_ts": claimed_ts},
            timeout=30
        )
        if not finish_resp.ok:
            print(f"::error::DO finish вернул {finish_resp.status_code}: {finish_resp.text}", file=sys.stderr)
            return 1
        finish_data = finish_resp.json()
        if not finish_data.get("accepted", False):
            print("::error::CAS не прошёл — ватчдог уже вернул сообщение в new или его забрала другая проходка", file=sys.stderr)
            # Не отправляем ответ — джоб потерял владение
            return 0
    except Exception as e:
        print(f"::error::не смог записать результат в DO: {e}", file=sys.stderr)
        return 1
    
    # CAS прошёл — отправляем ответ в канал
    if source == "telegram" and chat_id:
        delivered = send_telegram(response_text, telegram_bot_token, chat_id)
        if not delivered:
            print(f"::warning::ответ в Telegram (chat_id={chat_id}) не доставлен", file=sys.stderr)
            # Не откатываем результат — окно "ноль ответов" видно в messages.result
    elif source == "api" and chat_id:
        # Для dsh-edge — пока заглушка, реализуется вместе с требованием 9
        print(f"::notice::ответ для dsh-edge (session_id={chat_id}): {response_text}")
    else:
        # Локальная сессия / прочие — логируем
        print(f"::notice::ответ для {source} (chat_id={chat_id}): {response_text}")
    
    print(f"Готово: action={action}, response_sent={source}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Оркестратор сообщений")
    sub = parser.add_subparsers(dest="cmd", required=True)
    
    p_gather = sub.add_parser("gather", help="Сбор фактов и промпт")
    p_gather.add_argument("--message-id", required=True)
    p_gather.add_argument("--text", required=True)
    p_gather.add_argument("--chat-id", required=True)
    p_gather.add_argument("--source", required=True)
    p_gather.add_argument("--out", required=True)
    p_gather.set_defaults(func=cmd_gather)
    
    p_verdict = sub.add_parser("verdict", help="Обработка ответа и запись результата")
    p_verdict.add_argument("--message-id", required=True)
    p_verdict.add_argument("--chat-id", required=True)
    p_verdict.add_argument("--claimed-ts", required=True)
    p_verdict.add_argument("--source", required=True)
    p_verdict.add_argument("--answer", required=True)
    p_verdict.add_argument("--harness-url", required=True)
    p_verdict.add_argument("--hands-token", required=True)
    p_verdict.add_argument("--telegram-bot-token", required=True)
    p_verdict.add_argument("--telegram-chat-id", required=True)
    p_verdict.set_defaults(func=cmd_verdict)
    
    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    import subprocess
    sys.exit(main())