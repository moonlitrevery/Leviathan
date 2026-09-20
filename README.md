# Leviathan Bot

Fundação de um bot de Discord em Python 3.12 com [discord.py](https://discordpy.readthedocs.io) 2.x,
gerenciada com [uv](https://docs.astral.sh/uv/). Nesta etapa existe só a infraestrutura:
configuração validada, banco SQLite com migrations versionadas, carga automática de cogs,
sync instantâneo de slash commands na guild de desenvolvimento e tratamento global de erros.

## Estrutura

```
leviathan/
  __main__.py     # ponto de entrada + configuração de logging
  bot.py          # subclasse de commands.Bot, setup_hook, handler global de erro
  config.py       # leitura e validação do .env
  db.py           # pool aiosqlite + init_db() com migrations versionadas
  cogs/
    core.py       # /ping e /reload
```

## Pré-requisitos

- [uv](https://docs.astral.sh/uv/getting-started/installation/) instalado (ele baixa o Python 3.12 sozinho)
- Uma aplicação criada em https://discord.com/developers/applications

No portal do Discord, aba **Bot**, ligue os dois intents privilegiados que o bot declara:

- `MESSAGE CONTENT INTENT`
- `SERVER MEMBERS INTENT`

Depois, em **OAuth2 > URL Generator**, marque os escopos `bot` e `applications.commands`
e use a URL gerada para convidar o bot ao seu servidor de testes.

## Como rodar

```bash
# 1. instalar as dependências (cria .venv com Python 3.12)
uv sync

# 2. criar o seu .env a partir do exemplo
cp .env.example .env     # no PowerShell: Copy-Item .env.example .env

# 3. preencher DISCORD_TOKEN e GUILD_ID no .env

# 4. subir o bot
uv run python -m leviathan
```

Se faltar alguma variável, o bot sai com uma mensagem explicando o que corrigir,
sem traceback. No boot bem-sucedido os logs mostram o banco aberto, as migrations
aplicadas, os cogs carregados e a quantidade de comandos sincronizados.

## Variáveis de ambiente

| Variável | Obrigatória | Descrição |
| --- | --- | --- |
| `DISCORD_TOKEN` | sim | Token do bot (aba Bot > Reset Token) |
| `GUILD_ID` | sim | ID do servidor de desenvolvimento onde os comandos são sincronizados |
| `DATABASE_PATH` | não | Caminho do SQLite; padrão `data/leviathan.db` |

## Comandos

| Comando | Quem pode usar | O que faz |
| --- | --- | --- |
| `/ping` | todos | Mostra a latência do gateway e o ida-e-volta da API |
| `/reload <cog>` | dono do bot | Recarrega um cog e re-sincroniza os comandos, sem reiniciar |

## Adicionando uma feature

1. Crie `leviathan/cogs/minha_feature.py` com uma classe `commands.Cog` e uma função
   `async def setup(bot)`. O `setup_hook` varre a pasta, então não há lista de cogs para editar.
2. Se a feature precisar de tabelas, acrescente uma `Migration` nova no fim de `MIGRATIONS`
   em [db.py](leviathan/db.py), com a próxima versão livre. Nunca edite uma migration já
   aplicada — o `init_db()` roda só o que ainda não está em `schema_version`.
3. Com o bot no ar, `/reload minha_feature` aplica as mudanças de código na hora
   (migrations novas continuam exigindo restart).

## Notas de desenvolvimento

- O sync é feito com `tree.copy_global_to(guild=...)` + `tree.sync(guild=...)`: comandos de
  guild aparecem na hora, enquanto o sync global leva até uma hora para propagar.
- O banco roda em modo WAL com `foreign_keys` ligado. `data/` está no `.gitignore`.
- Logging em nível `INFO` com timestamp; `discord.gateway` fica em `WARNING` para
  não poluir a saída com heartbeats.
