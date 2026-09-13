// Мутационное доказательство гвардии «каталог не отравляет литерал namespace»
// (находка ревью PR #453, п.2): зелёная на чистом каталоге, красная, если в
// каталог просочился литерал "llm-pi-ai" (симуляция инцидента plugin-manager
// 0.1.4). Запуск: node --test dsh-edge/verify-catalog-namespace-literal.test.mjs
import { mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { describe, it } from 'node:test';
import assert from 'node:assert/strict';
import { findLiteralHits, NAMESPACE_LITERAL } from './verify-catalog-namespace-literal.mjs';

function fakeFile(contents) {
  const dir = mkdtempSync(join(tmpdir(), 'catalog-ns-guard-'));
  const path = join(dir, 'catalog.json');
  writeFileSync(path, contents);
  return { dir, path };
}

describe('verify-catalog-namespace-literal (гвардия каталога)', () => {
  it('зелёная: каталог не упоминает приватный литерал апстрима', () => {
    const { dir, path } = fakeFile('{"plugins":[{"id":"combo-auto","brief":"поверх провайдеров реестра"}]}');
    try {
      assert.deepEqual(findLiteralHits([path]), []);
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  });

  it('красная: литерал просочился в бриф каталога (мутация — прецедент plugin-manager 0.1.4)', () => {
    const { dir, path } = fakeFile(`{"plugins":[{"id":"x","brief":"settings namespace ${NAMESPACE_LITERAL}"}]}`);
    try {
      const hits = findLiteralHits([path]);
      assert.equal(hits.length, 1);
      assert.equal(hits[0].path, path);
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  });

  it('красная: файл каталога недоступен — громкий отказ, не тихий пропуск', () => {
    const hits = findLiteralHits(['/nonexistent-catalog-ns-guard.json']);
    assert.equal(hits.length, 1);
    assert.match(hits[0].error, /ENOENT/);
  });

  it('реальные dsh-edge/plugins.json и dsh-edge/plugins-catalog.json репозитория сейчас чисты', () => {
    assert.deepEqual(findLiteralHits(), []);
  });
});
