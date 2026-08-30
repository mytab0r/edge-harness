window.__ModuleLoader__.load({
	id: "@deepseek-ai/dsh-client-ui-brand-official",
	factory: (require) => {
		var module = { exports: {} };
		var exports = module.exports;
		Object.defineProperty(exports, Symbol.toStringTag, { value: "Module" });
		let react_jsx_runtime = require("react/jsx-runtime");
		//#region Бренд морды (issue #109)
		//
		// Этот файл — замена апстримного client.js пакета
		// @deepseek-ai/dsh-client-ui-brand-official: оригинал рендерил кита
		// (FishLogo) и SVG-вордмарк «deepseek» (BrandWordmark — буквы нарисованы
		// путями, поэтому строковый ребренд #101 их не достал). Форма сохранена
		// один в один: те же три слота, та же обёртка ModuleLoader, тот же
		// экспорт apply/inject — меняется только отрисовка.
		//
		// Текст наследует стили контейнера .brandName сайдбара
		// (18px/600/letter-spacing .04em) — inline-стили дублируют их, потому
		// что слот может смонтироваться вне сайдбара (hero-экран).
		//
		// Деплой-шаг «Бренд-плагин и favicon морды» перезаписывает этим файлом
		// собранный dist/plugins/@deepseek-ai/dsh-client-ui-brand-official/client.js
		// и падает громко, если апстрим сменил форму оригинала.
		function BrandName() {
			return react_jsx_runtime.jsx("span", {
				style: {
					fontSize: 18,
					fontWeight: 600,
					lineHeight: "24px",
					letterSpacing: "0.04em",
					whiteSpace: "nowrap",
				},
				children: "mytab0r",
			});
		}
		function NoMark() {
			return null;
		}
		//#endregion
		//#region lib/types/client/index.js
		/** Required service: the UI slot registry. */
		const inject = ["slots"];
		/**
		* Fill every shipped brand slot as one declaration-aware registration set.
		* @param ctx - Client root context.
		*/
		function apply(ctx) {
			ctx.slots.inject("sidebar.brand.mark", () => ctx.slots.inject("sidebar.brand.name", () => ctx.slots.inject("conversation.hero.brand.mark", function* () {
				yield ctx.slots.register({ name: "sidebar.brand.mark" }, NoMark);
				yield ctx.slots.register({ name: "sidebar.brand.name" }, BrandName);
				yield ctx.slots.register({ name: "conversation.hero.brand.mark" }, BrandName);
			})));
		}
		//#endregion
		exports.apply = apply;
		exports.inject = inject;
		return module.exports;
	}
});
