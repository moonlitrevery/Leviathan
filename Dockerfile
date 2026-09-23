# Imagem do Leviathan Bot para a VM ARM (aarch64) do Oracle Cloud Always Free.
#
# A base é Debian (slim) e não Alpine de propósito: as wheels de aarch64 do
# Pillow e do aiohttp são manylinux, ou seja, glibc. No Alpine, que usa musl, a
# instalação cairia no sdist e teria de compilar. Como está, nada compila.
#
# Build na própria VM:
#     docker compose build
# Build multi-arch a partir de uma máquina x86:
#     docker buildx build --platform linux/amd64,linux/arm64 -t leviathan .

# 3.12 é a versão fixada no uv.lock. De 3.13 em diante o discord.py passa a
# depender do audioop-lts, que não está no lock.
FROM python:3.12-slim-bookworm

# tini vira o PID 1 e repassa o SIGTERM do "docker stop" ao Python, para o
# close() do bot conseguir fechar a sessão HTTP e o banco antes de morrer.
# ca-certificates é obrigatório: tudo que o bot fala é HTTPS.
# sqlite3 fica na imagem para o backup poder rodar de dentro do contêiner.
RUN apt-get update \
    && apt-get install --no-install-recommends -y tini ca-certificates sqlite3 \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --system --uid 10001 --home /app leviathan

COPY --from=ghcr.io/astral-sh/uv:0.11.29 /uv /usr/local/bin/uv

ENV PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

# As dependências ficam numa camada própria: mexer no código do bot não refaz a
# instalação. --locked falha se o lock estiver desatualizado, em vez de resolver
# versões diferentes das testadas; --no-install-project porque o projeto não tem
# build-system e roda direto do diretório, como no desenvolvimento.
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project

# leviathan/ carrega junto os assets que o bot lê em tempo de execução: a fonte
# dos cards de citação e o data/links.json.
COPY leviathan/ ./leviathan/
# O script de backup roda de dentro do contêiner (via docker exec), porque é lá
# que o volume com o banco está montado e o sqlite3 já existe.
COPY deploy/backup.sh ./deploy/backup.sh

# Banco e cache de emoji. Criar a pasta aqui, já com o dono certo, é o que faz o
# volume nomeado do compose nascer pertencendo ao usuário do bot.
# O chmod é necessário porque o bit de execução não sobrevive ao checkout no
# Windows, onde este projeto é desenvolvido.
RUN chmod +x /app/deploy/backup.sh     && mkdir -p /app/data     && chown -R leviathan:leviathan /app

USER leviathan

# Sem EXPOSE: o bot só faz conexão de saída (gateway do Discord, YouTube,
# MangaDex, AniList, Last.fm). Nenhuma porta precisa ser aberta.

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "-m", "leviathan"]
