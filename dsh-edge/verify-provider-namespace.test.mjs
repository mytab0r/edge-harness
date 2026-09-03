// Мутационное доказательство гвардии литерала (tasks.md раздел 2 #114):
// гвардия обязана быть зелёной на dist с литералом "llm-pi-ai" и красной
// с ожидаемым сообщением на dist, где литерал переименован (симуляция
// апстримного релиза, убившего кнопку). Тест — часть repo-ci, не разовая
// ручная проверка.
// Запуск: node --test dsh-edge/verify-provider-namespace.test.mjs
import { spawnSync } from 'node:child_process';
import { mkdtempSync, mkdirSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { describe, it } from 'node:test';
import assert from 'node:assert/strict';

const guard = fileURLToPath(new URL('./verify-provider-namespace.mjs', import.meta.url));

/** Фейковый dist: один бандл с заданным содержимым. */
function fakeDist(contents) {
  const dir = mkdtempSync(join(tmpdir(), 'provider-ns-guard-'));
  mkdirSync(join(dir, 'plugins', 'x'), { recursive: true });
  writeFileSync(join(dir, 'plugins', 'x', 'client.js'), contents);
  return dir;
}

function runGuard(distDir) {
  return spawnSync(process.execPath, [guard, distDir], { encoding: 'utf8' });
}

describe('verify-provider-namespace (гвардия литерала llm-pi-ai)', () => {
  it('зелёная: литерал на месте в бандле', () => {
    const dist = fakeDist('const NS$1 = "llm-pi-ai"; /* CustomProviderCard */');
    try {
      const run = runGuard(dist);
      assert.equal(run.status, 0, `гвардия красная на живом литерале: ${run.stderr}`);
      assert.match(run.stdout, /llm-pi-ai/);
    } finally {
      rmSync(dist, { recursive: true, force: true });
    }
  });

  it('красная с ожидаемым сообщением: литерал переименован (мутация апстрима)', () => {
    const dist = fakeDist('const NS$1 = "llm-pi-renamed";');
    try {
      const run = runGuard(dist);
      assert.equal(run.status, 1, 'гвардия пропустила переименованный литерал — мутация не поймана');
      assert.match(run.stderr, /Литерал "llm-pi-ai" не найден/);
      assert.match(run.stderr, /Деплой остановлен/);
    } finally {
      rmSync(dist, { recursive: true, force: true });
    }
  });

  it('красная: пустой dist (upstream сменил состав ростера)', () => {
    const dist = mkdtempSync(join(tmpdir(), 'provider-ns-guard-empty-'));
    try {
      const run = runGuard(dist);
      assert.equal(run.status, 1);
      assert.match(run.stderr, /не найден ни в одном файле/);
    } finally {
      rmSync(dist, { recursive: true, force: true });
    }
  });

  it('красная: каталога нет — громкий отказ в формате деплоя, не сырой стек', () => {
    const run = runGuard('/nonexistent-provider-ns-guard-dir');
    assert.equal(run.status, 1);
    assert.match(run.stderr, /::error::Не удалось обойти/);
  });
});
