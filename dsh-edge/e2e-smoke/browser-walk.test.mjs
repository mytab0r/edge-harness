// Юнит-тест closeTourOverlay (класс флейка #1066, run 34700537627):
// locator.click: Timeout на кнопке «Settings», перехваченной маской
// оверлей-тура первого визита. Старая версия глушила отказ клика
// `.catch(() => {})` и ждала фиксированные 500 мс вместо факта закрытия —
// эта проверка доказывает МУТАЦИЕЙ ПОВЕДЕНИЯ, что новая версия (а) не
// глушит отказ клика молча и (б) ждёт именно факт (кнопка «Continue» ушла
// в hidden), а не паузу вслепую.
//
// Честная граница: настоящий Playwright Locator здесь не участвует — фейк
// duck-typed под ту часть API (count/click/waitFor), которую использует
// closeTourOverlay. Это не «тест кормит прод-форму данных» в буквальном
// смысле (нет реального DOM/CSS-маски), а модель контракта Locator, потому
// что полноценный браузерный прогон в юнит-тесте нечестен (нужен настоящий
// Chromium + собранная морда). Мутация, которую тест реально ловит: если
// откатить closeTourOverlay на старую форму (клик + .catch(()=>{}) + фикс.
// пауза), тест «маска не закрылась» ниже перестаёт падать вообще — функция
// молча вернула бы управление, ничего не бросив, ровно тот силент-баг,
// который держал PR #1066.
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { closeTourOverlay } from './browser-walk.mjs'

// Фейковый Locator: несёт только count/click/waitFor — ровно то, что
// closeTourOverlay вызывает у page.getByRole('button', { name: 'Continue' }).
function makeFakeTourLocator({ count = 1, clickThrows = false, waitForThrows = false } = {}) {
  const calls = { count: 0, click: 0, waitFor: 0 }
  return {
    calls,
    async count() {
      calls.count += 1
      return count
    },
    async click(opts) {
      calls.click += 1
      if (clickThrows) {
        throw new Error(`locator.click: Timeout ${opts?.timeout}ms exceeded.`)
      }
    },
    async waitFor(opts) {
      calls.waitFor += 1
      assert.equal(opts?.state, 'hidden', 'closeTourOverlay обязан ждать именно state: "hidden"')
      if (waitForThrows) {
        throw new Error(`locator.waitFor: Timeout ${opts?.timeout}ms exceeded waiting for state "hidden".`)
      }
    },
  }
}

test('тура нет (count() === 0) — ничего не кликает, возвращает false', async () => {
  const locator = makeFakeTourLocator({ count: 0 })
  const result = await closeTourOverlay(locator)
  assert.equal(result, false)
  assert.equal(locator.calls.click, 0)
  assert.equal(locator.calls.waitFor, 0)
})

test('тур есть, клик прошёл, маска реально исчезла — закрыт, возвращает true', async () => {
  const locator = makeFakeTourLocator({ count: 1 })
  const result = await closeTourOverlay(locator)
  assert.equal(result, true)
  assert.equal(locator.calls.click, 1)
  assert.equal(locator.calls.waitFor, 1)
})

test('клик по «Continue» не прошёл — падает с точной причиной, не глушится', async () => {
  const locator = makeFakeTourLocator({ count: 1, clickThrows: true })
  await assert.rejects(
    () => closeTourOverlay(locator),
    (error) => {
      assert.match(error.message, /клик по кнопке «Continue» не прошёл/)
      return true
    },
  )
  // Раз клик не прошёл — ждать закрытия маски уже нечего.
  assert.equal(locator.calls.waitFor, 0)
})

// Ключевая мутация #1066: клик РЕЗОЛВИТСЯ (Playwright считает, что кликнул),
// но кнопка «Continue»/маска фактически не ушли (тур появился позже
// проверки count(), анимация закрытия зависла и т.п.) — старый код тут
// молча продолжал бы работу (.catch(()=>{}) + waitForTimeout(500)) и падал
// бы ПОЗЖЕ на клике «Settings» с вводящим в заблуждение «кнопка не найдена».
// Новая версия обязана назвать ФАКТ прямо здесь.
test('клик прошёл, но маска не исчезла за таймаут — падает с фактом, а не с паузой вслепую', async () => {
  const locator = makeFakeTourLocator({ count: 1, waitForThrows: true })
  await assert.rejects(
    () => closeTourOverlay(locator),
    (error) => {
      assert.match(error.message, /оверлей-тур не закрылся/)
      assert.doesNotMatch(error.message, /кнопка.*Settings/i, 'сообщение не должно называть Settings — причина в туре, а не в Settings')
      return true
    },
  )
  assert.equal(locator.calls.click, 1)
  assert.equal(locator.calls.waitFor, 1)
})
