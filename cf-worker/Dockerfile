# Dev-среда воркера в Linux-контейнере: та же среда, что CI и прод, без платформенной
# специфики (CRLF, пути, блокировки портов Windows). Прод-образ это НЕ продакшен-рантайм:
# внутри тот же wrangler dev, поэтому канарейка здесь гоняет код до деплоя.
FROM node:24-bookworm-slim

WORKDIR /app

COPY package.json package-lock.json ./
RUN npm ci

COPY . .
RUN [ -f .dev.vars ] || cp .dev.vars.example .dev.vars

EXPOSE 8808
CMD ["npx", "wrangler", "dev", "--ip", "0.0.0.0", "--port", "8808"]
