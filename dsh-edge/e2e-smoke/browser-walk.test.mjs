// Юнит-тест closeTourOverlay (класс флейка #1066, run 34700537627; третья
// рука той же гонки — #1070, находка ai-review PR #1070):
// locator.click: Timeout на кнопке «Settings», перехваченной маской
// оверлей-тура первого визита. Старая версия глушила отказ клика
// `.catch(() => {})` и ждала фиксированные 500 мс вместо факта закрытия —
// эта проверка доказывает МУТАЦИЕЙ ПОВЕДЕНИЯ, что новая версия (а) не
// глушит отказ клика молча, (б) ждёт именно факт (кнопка «Continue» ушла в
// hidden), а не паузу вслепую, и (в) не путает мгновенный снимок
// «кнопки нет ПРЯМО СЕЙЧАС» с «тура не будет вовсе» — ждёт появления с
// потолком, а не проверяет `count()` один раз.
//
// Честная граница: настоящий Playwright Locator здесь не участвует — фейк
// duck-typed под ту часть API (click/waitFor), которую использует
// closeTourOverlay. Это не «тест кормит прод-форму данных» в буквальном
// смысле (нет реального DOM/CSS-маски), а модель контракта Locator, потому
// что полноценный браузерный прогон в юнит-тесте нечестен (нужен настоящий
// Chromium + собранная морда). Мутация, которую тест реально ловит: если
// откатить closeTourOverlay на старую форму (клик + .catch(()=>{}) + фикс.
// пауза), тест «маска не закрылась» ниже перестаёт падать вообще — функция
// молча вернула бы управление, ничего не бросив, ровно тот силент-баг,
// который держал PR #1066. Если откатить проверку появления на
// `count() === 0` (баг #1070), тест «тур появляется позже мгновенного
// снимка» ниже перестаёт падать — функция молча вернула бы false и не
// заметила тур, который смонтировался чуть позже.
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { closeTourOverlay } from './browser-walk.mjs'

// Фейковый Locator: несёт только click/waitFor — ровно то, что
// closeTourOverlay вызывает у page.getByRole('button', { name: 'Continue' }).
// `appear` управляет исходом ПЕРВОГО waitFor (ожидание появления, state:
// 'visible'): true — резолвится сразу, false — падает таймаутом (тура нет).
function makeFakeTourLocator({ appear = true, clickThrows = false, waitForHiddenThrows = false } = {}) {
  const calls = { click: 0, waitForVisible: 0, waitForHidden: 0 }
  return {
    calls,
    async click(opts) {
      calls.click += 1
      if (clickThrows) {
        throw new Error(`locator.click: Timeout ${opts?.timeout}ms exceeded.`)
      }
    },
    async waitFor(opts) {
      if (opts?.state === 'visible') {
        calls.waitForVisible += 1
        if (!appear) {
          throw new Error(`locator.waitFor: Timeout ${opts?.timeout}ms exceeded waiting for state "visible".`)
        }
        return
      }
      assert.equal(opts?.state, 'hidden', 'closeTourOverlay обязан ждать состояние "visible" или "hidden", не третье')
      calls.waitForHidden += 1
      if (waitForHiddenThrows) {
        throw new Error(`locator.waitFor: Timeout ${opts?.timeout}ms exceeded waiting for state "hidden".`)
      }
    },
  }
}

test('тура нет (не появился за appearTimeoutMs) — ничего не кликает, возвращает false', async () => {
  const locator = makeFakeTourLocator({ appear: false })
  const result = await closeTourOverlay(locator)
  assert.equal(result, false)
  assert.equal(locator.calls.click, 0)
  assert.equal(locator.calls.waitForHidden, 0)
})

// Мутация #1070: тур появляется чуть позже мгновенного снимка. Фейк тут не
// отличим от «тур есть сразу» (waitFor резолвится по факту), но именно это
// и есть контракт — appearTimeoutMs существует ровно ради такого случая, а
// проверка count() один раз его не ловила (см. докстринг сверху).
test('тур появляется (waitFor visible резолвится по факту), клик прошёл — закрыт, возвращает true', async () => {
  const locator = makeFakeTourLocator({ appear: true })
  const result = await closeTourOverlay(locator)
  assert.equal(result, true)
  assert.equal(locator.calls.waitForVisible, 1)
  assert.equal(locator.calls.click, 1)
  assert.equal(locator.calls.waitForHidden, 1)
})

test('клик по «Continue» не прошёл — падает с точной причиной, не глушится', async () => {
  const locator = makeFakeTourLocator({ appear: true, clickThrows: true })
  await assert.rejects(
    () => closeTourOverlay(locator),
    (error) => {
      assert.match(error.message, /клик по кнопке «Continue» не прошёл/)
      return true
    },
  )
  // Раз клик не прошёл — ждать закрытия маски уже нечего.
  assert.equal(locator.calls.waitForHidden, 0)
})

// Ключевая мутация #1066: клик РЕЗОЛВИТСЯ (Playwright считает, что кликнул),
// но кнопка «Continue»/маска фактически не ушли (анимация закрытия
// зависла и т.п.) — старый код тут молча продолжал бы работу
// (.catch(()=>{}) + waitForTimeout(500)) и падал бы ПОЗЖЕ на клике
// «Settings» с вводящим в заблуждение «кнопка не найдена». Новая версия
// обязана назвать ФАКТ прямо здесь.
test('клик прошёл, но маска не исчезла за таймаут — падает с фактом, а не с паузой вслепую', async () => {
  const locator = makeFakeTourLocator({ appear: true, waitForHiddenThrows: true })
  await assert.rejects(
    () => closeTourOverlay(locator),
    (error) => {
      assert.match(error.message, /оверлей-тур не закрылся/)
      assert.doesNotMatch(error.message, /кнопка.*Settings/i, 'сообщение не должно называть Settings — причина в туре, а не в Settings')
      return true
    },
  )
  assert.equal(locator.calls.click, 1)
  assert.equal(locator.calls.waitForHidden, 1)
})
