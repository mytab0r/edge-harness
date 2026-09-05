#!/usr/bin/env python3
"""Гвардия класса #43 «второй пакетный менеджер поверх pnpm-дерева standalone».

Класс (белое пятно #43, закрыт маршрутом dsh-edge-plugin-system, design —
openspec/changes/dsh-edge-plugin-system/design.md, раздел «Отвергнутые
варианты», пункт «`npm install` плагинов в standalone»): standalone-дерево
`apps/dsh-edge/standalone` живёт на pnpm с семью ОБЯЗАТЕЛЬНЫМИ для Workers
`pnpm patch`-патчами (`standalone/patches/audit.json` — причины:
worker-несовместимости). `npm install` поверх такого дерева переопределяет
`node_modules`, игнорируя pnpm-патчи, — сборка МОЖЕТ пройти зелёной, но с
бепатчными пакетами, то есть молча неверным воркером. Обход через
`--legacy-peer-deps` делает шаг зелёным при всё том же молча-неверном
результате: маскирует проблему, а не решает.

Вторжение npm сюда уже случалось (ERESOLVE на фантомном peer в run
33221096200); сейчас маршрут правильный (`pnpm add <tgz>` + проверка
patchedDependencies), и эта гвардия делает его возврат к npm механически
невозможным — регресс красит CI, а не собирает тихо неверную морду.

Правила:
  1. deploy-dsh-edge.yml не ставит пакеты npm'ом: ни `npm install`, ни его
     шорткат `npm i`, ни `npm ci`, ни `npm add` — в ЛЮБОЙ форме записи и
     позиции: блочная `run: |` (голая строка), инлайн `- run: npm …`,
     цепочка `cd … && npm install` (это единственный workflow, работающий
     в pnpm-дереве dsh-edge). `npm pack` легален — он скачивает tarball
     префаба во временный каталог и node_modules не трогает.
  2. `--legacy-peer-deps` запрещён ВЕЗДЕ, где он вообще может появиться:
     все .github/workflows/*.yml|yaml и scripts/**/*.sh. Флаг — не решение
     peer-конфликта, а его маскировка (тот же класс); peer-конфликт чинится
     явным пином/исключением, а не молчаливым «поставь как нибудь».
     cf-worker и его `npm ci` в deploy-worker.yml не тронуты правилом 1:
     там своё npm-дерево с package-lock, pnpm-патчей нет.
  3. pnpm-маршрут плагинов обязан оставаться на месте: шаг
     `pnpm add --save-exact` (единственный установщик плагинов) и следующая
     за ним проверка `patchedDependencies` в pnpm-workspace.yaml. Исчезла
     проверка — `pnpm add` снова может потерять патчи молча (класс #43),
     и CI обязан об этом крикнуть, а не довериться.

Область действия правила 1 — ровно deploy-dsh-edge.yml, потому что путь
`clone/apps/dsh-edge/standalone` существует только внутри него; иных мест,
где скрипты этого репозитория ставят пакеты в pnpm-дерево, нет (проверено
grep по scripts/ при написании гвардии). Сопредельные npm-употребления —
`npm install -g ./*.tgz` в scripts/lib/dsh-ci.sh (глобальная установка CLI
раннера из локальных tarball'ов, не pnpm-дерево) — правилами не запрещены.

Запуск: python -m pytest scripts/lib/test_dsh_edge_pnpm_guard.py -q
"""

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = REPO_ROOT / ".github" / "workflows"
DEPLOY_DSH_EDGE = WORKFLOWS / "deploy-dsh-edge.yml"

# npm как УСТАНОВЩИК пакетов (меняет node_modules): install/ci/add — включая
# шорткат `i`. В deploy-dsh-edge.yml легального npm-установщика нет вовсе
# (npm участвует только как `npm pack` — скачать tarball префаба, он в
# чередование не входит), поэтому позиционные якоря не нужны: ловится ЛЮБОЕ
# вхождение в строках кода. История правила — три находки AI-ревью #313:
# (1) шорткат `npm i` и инлайн `- run: npm install`, (2) цепочка
# `cd … && npm install`, (3) САМАЯ ОБЫЧНАЯ блочная форма — голая строка
# `npm install …` внутри `run: |`-блока: все три проходили позиционные
# версии якоря молча. Негативный просмотр `(?<![A-Za-z])` отсекает `pnpm`
# (перед `npm` буква `p`); `\b` после `i` не цепляет `npm init`/`npm info`.
# Цена честности: npm-install в ПРОЗЕ кодовой строки (echo «используй
# npm install») тоже красится — в этом workflow такого писать нельзя, и
# ложный красный громок и чинится в секунды, а не маскирует класс #43.
# Отсечение комментариев (_code_lines_numbered) исключает прозу комментариев.
NPM_INSTALLER_RE = re.compile(r"(?<![A-Za-z])npm\s+(?:install|i|ci|add)\b")

# Маскирующий флаг peer-конфликтов — запрещён в любых файлах, которые
# вообще могут исполняться (workflow + shell-скрипты репозитория).
MASKING_FLAG = "--legacy-peer-deps"

# Правило 3: единственный легальный установщик плагинов и его страховка.
PNPM_ADD_RE = re.compile(r"^\s*pnpm add --save-exact\b", re.M)
PATCHED_GUARD_RE = re.compile(
    r"^\s*grep -q [\"']patchedDependencies[\"'] pnpm-workspace\.yaml", re.M
)


def _code_lines(text: str) -> str:
    """Строки кода без комментариев (YAML `# …` и shell `# …`): гвардия судит
    по командам, не по прозе. Комментарий с советом «можно было бы npm install»
    не должен красить CI — и не должен его зеленить (мутация #246 показала,
    что подстрочный матч по комментарию делает гвардию фикцией)."""
    return "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("#")
    )


def _code_lines_numbered(text: str) -> list[tuple[int, str]]:
    """Те же строки кода, но с НАСТОЯЩИМИ номерами строк файла — чтобы красное
    сообщение вело ровно на строку workflow'а, а не на номер в срезанном
    списке (комментарии выкинуты, номера сохранены)."""
    return [
        (idx, line)
        for idx, line in enumerate(text.splitlines(), 1)
        if not line.lstrip().startswith("#")
    ]


def _workflow_files() -> list[Path]:
    # GitHub Actions грузит workflows из .yml И .yaml — гвардия обязана видеть
    # оба (находка AI-ревью #146: файл evil.yaml проходил мимо односуффиксного
    # скана).
    return sorted(
        path for path in WORKFLOWS.iterdir() if path.suffix in {".yml", ".yaml"}
    )


def test_deploy_dsh_edge_never_installs_packages_with_npm():
    """Правило 1: npm — не установщик пакетов в workflow деплоя dsh-edge."""
    assert DEPLOY_DSH_EDGE.exists(), (
        "deploy-dsh-edge.yml исчез или переименован — обнови путь в гвардии "
        "сознательной правкой, а не молчаливым обходом"
    )
    text = DEPLOY_DSH_EDGE.read_text(encoding="utf-8")
    offenders = [
        f"строка {idx} — {line.strip()}"
        for idx, line in _code_lines_numbered(text)
        if NPM_INSTALLER_RE.search(line)
    ]
    assert not offenders, (
        "deploy-dsh-edge.yml ставит пакеты npm'ом (класс #43): npm "
        "переопределяет node_modules поверх pnpm-дерева standalone и теряет "
        "семь обязательных для Workers pnpm-патчей — сборка пройдёт зелёной, "
        f"но с бепатчными пакетами (молча неверная морда): {offenders}. "
        "Зависимости dsh-edge ставь pnpm (pnpm add/install); npm pack для "
        "скачивания префаба легален — дерево он не трогает"
    )


def test_no_legacy_peer_deps_masking_anywhere():
    """Правило 2: маскирующий флаг peer-конфликтов запрещён во всех workflow
    и shell-скриптах. Peer-конфликт чинится явно, а не глушится."""
    offenders = []
    for path in _workflow_files() + sorted((REPO_ROOT / "scripts").rglob("*.sh")):
        rel = str(path.relative_to(REPO_ROOT)).replace("\\", "/")
        for idx, line in _code_lines_numbered(path.read_text(encoding="utf-8")):
            if MASKING_FLAG in line:
                offenders.append(f"{rel}:{idx} — {line.strip()}")
    assert not offenders, (
        f"{MASKING_FLAG} в репозитории (класс #43): флаг делает шаг зелёным "
        "при неразрешённом peer-конфликте — маскировка вместо решения, "
        f"{offenders}. Конфликт peers чинится явным пином/исключением "
        "фантомного peer'а в исходниках, а не флагом"
    )


def test_deploy_dsh_edge_keeps_pnpm_plugin_route():
    """Правило 3: pnpm-маршрут плагинов (add --save-exact + проверка
    patchedDependencies) обязан оставаться в workflow — исчезновение проверки
    возвращает класс «pnpm add потерял патчи молча»."""
    code = _code_lines(DEPLOY_DSH_EDGE.read_text(encoding="utf-8"))
    assert PNPM_ADD_RE.search(code), (
        "deploy-dsh-edge.yml потерял `pnpm add --save-exact` — установщик "
        "плагинов ушёл с pnpm-маршрута (класс #43: чужой пакетный менеджер "
        "поверх pnpm-дерева теряет обязательные патчи Workers)"
    )
    assert PATCHED_GUARD_RE.search(code), (
        "deploy-dsh-edge.yml потерял проверку patchedDependencies после "
        "pnpm add — `pnpm add` снова может потерять pnpm-патчи молча "
        "(класс #43), и это обязано остаться красным шагом деплоя"
    )


# Мутации, которыми доказана гвардия (каждая — красный тест, возврат — зелёный;
# перечень краснеющих тестов в каждой записи — проверенный факт, а не ожидание:
# неточная запись ловилась AI-ревью #313, раунд 3):
#
#   М1 (правила 1+2+3): в deploy-dsh-edge.yml заменить
#     `pnpm add --save-exact "${args[@]}"` на
#     `npm install --legacy-peer-deps "${args[@]}"` —
#     красны test_deploy_dsh_edge_never_installs_packages_with_npm
#     (голая строка npm install в run-блоке), test_no_legacy_peer_deps_masking_anywhere
#     (флаг), test_deploy_dsh_edge_keeps_pnpm_plugin_route (pnpm add исчез).
#   М2 (правило 3, вторая половина): удалить только строку
#     `grep -q "patchedDependencies" pnpm-workspace.yaml …` —
#     красен исключительно test_deploy_dsh_edge_keeps_pnpm_plugin_route.
#   М3 (правило 2, scripts-часть): дописать в конец scripts/lib/dsh-ci.sh
#     no-op-команду `: "--legacy-peer-deps"` (строка с флагом в комментарии
#     красить не должна — комментарии отсечены) — красен
#     test_no_legacy_peer_deps_masking_anywhere; удалить строку.
#   М4 (правило 1, инлайн-форма и шорткат — находка AI-ревью #313, раунд 1):
#     дописать в deploy-dsh-edge.yml шаг `- run: npm i pnpm` — красен
#     test_deploy_dsh_edge_never_installs_packages_with_npm; а
#     `- run: npm init -y` КРАСИТЬ НЕ ДОЛЖЕН (\b после `i`; init не ставит
#     пакеты) — проверить и на него; удалить строки.
#   М5 (правило 1, ложный позитив): на неизменённом дереве не красят:
#     `pnpm add --save-exact` (перед `npm` в `pnpm` стоит `p`),
#     `npm pack "$SPEC"`, `- uses: actions/checkout@v7`, `node --check`,
#     `npm init -y`. НП: `echo "… npm install …"` теперь КРАСИТ — это
#     осознанная цена правила «в этом workflow npm-установщика нет вовсе»
#     (см. комментарий у NPM_INSTALLER_RE): громкий ложный красный чинится
#     в секунды, тихий пропуск класса #43 — нет.
#   М6 (правило 1, cd-цепочка — находка AI-ревью #313, раунд 2): в
#     deploy-dsh-edge.yml в любом run-блоке дописать строку
#     `          cd clone/apps/dsh-edge/standalone && npm install` — красен
#     test_deploy_dsh_edge_never_installs_packages_with_npm; удалить строку.
#   М7 (правило 1, блочная форма — находка AI-ревью #313, раунд 3): дописать
#     в run-блок голую строку `          npm install foo.tgz` (pnpm-маршрут
#     и флаг не тронуты) — красен
#     test_deploy_dsh_edge_never_installs_packages_with_npm; удалить строку.
