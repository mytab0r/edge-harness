window.__ModuleLoader__.load({ id: "@edge-harness/dsh-plugin-hello", factory: (require) => {
var module = { exports: {} }; var exports = module.exports;

const inject = [];

function apply(ctx) {
  ctx.effect(() => {
    document.documentElement.dataset.edgePluginHello = "mounted";
    console.info("[edge-plugin:hello] client plugin mounted");
    return () => {
      delete document.documentElement.dataset.edgePluginHello;
    };
  }, "edge-plugins:hello mounted marker");
}

exports.inject = inject;
exports.apply = apply;
return module.exports; } });
