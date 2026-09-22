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
  emoji.py        # normalização de emoji, compartilhada entre cogs
  cogs/
    core.py       # /ping e /reload
    piadas.py     # gatilhos de texto e contadores por reação
    links.py      # /pirata e /sistema, lidos do JSON
    quotes.py     # cards de citação em imagem (Pillow)
  data/
    links.json    # conteúdo dos comandos de links (versionado, editável)
  assets/
    fonts/        # fonte embarcada, usada pelos cards de citação
scripts/
  preview_quote.py  # gera previews do card sem subir o bot
tests/
  test_piadas.py  # deduplicação dos contadores e casamento dos gatilhos
  test_quotes.py  # segmentação de emoji e medição do card
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
| `/gatilho add <palavra> <url> [substring]` | Gerenciar servidor | Cadastra ou atualiza uma palavra-gatilho |
| `/gatilho remove <palavra>` | Gerenciar servidor | Remove uma palavra-gatilho |
| `/gatilho list` | todos | Lista os gatilhos em vigor no servidor |
| `/contador criar <emoji> <nome> <canal_placar>` | Gerenciar servidor | Cria um contador alimentado por reações |
| `/contador list` | todos | Lista os contadores e seus totais |
| `/contador remover <nome>` | Gerenciar servidor | Apaga o contador e todo o histórico dele |
| `/contador rank <nome>` | todos | Ranking completo de quem mais recebeu a reação |
| `/pirata` | todos | Megathread do r/Piracy e os guias do FMHY |
| `/sistema [distro]` | todos | Links de instalação de um sistema; sem argumento, lista todos |
| `/quote config <emoji> <canal>` | Gerenciar servidor | Define o emoji que salva citações e o canal dos cards |
| `/quote random` | todos | Reposta o card de uma citação salva ao acaso |
| `/quote de <membro>` | todos | Citação ao acaso daquela pessoa |
| `/quote count` | todos | Total de citações e top 5 de quem mais foi citado |

## Cards de citação (`/quote`)

Reagir com o emoji configurado (sugestão: 📌) numa mensagem gera um card em imagem e
publica no canal de quotes. Configure antes com `/quote config 📌 #quotes` — sem
configuração, nada acontece.

**Cada mensagem vira card uma única vez.** A garantia é o `UNIQUE` em
`quotes.message_id`: a segunda reação esbarra nele e o `INSERT OR IGNORE` vira no-op,
mesmo que a pessoa remova e reaja de novo, ou que outra pessoa reaja depois. São
ignoradas: mensagens de bots, mensagens sem texto (só anexo) e reações dentro do
próprio canal de quotes.

O texto e o nome do autor ficam **gravados** na tabela `quotes`, não só referenciados:
a citação sobrevive à mensagem original ser apagada. É essa tabela que vai alimentar o
jogo de adivinhação depois.

Se o emoji escolhido já alimentar um contador do cog `piadas`, o `/quote config` avisa
— reagir dispararia as duas coisas ao mesmo tempo. O aviso não bloqueia: se for de
propósito, tudo bem.

### A imagem

Canvas 1200×630, fundo escuro sólido, avatar do autor recortado em círculo à esquerda
(baixado via aiohttp; se o download falhar, entra um círculo cinza). A citação aparece
entre aspas com quebra de linha automática e **tamanho de fonte que diminui sozinho**
conforme o texto cresce, de 52px até 20px, para sempre caber. Acima de 400 caracteres o
texto é cortado com reticências. Abaixo vêm o nome de exibição e, menor e mais apagada,
a data e o canal.

Pillow é bloqueante, então a renderização roda em `asyncio.to_thread` — sem isso, cada
card travaria o event loop do bot inteiro.

O texto salvo é o `clean_content` da mensagem: `<@123>` já vira `@Nome` e `<#456>` vira
`#canal`. É essa forma que vai para o banco, então o jogo de adivinhação futuro lê dali
sem risco de vazar o ID de ninguém.

**Emoji.** A Inter não tem glifos de emoji, então emoji não é desenhado como texto —
viraria quadrado. O texto é segmentado (com a biblioteca `emoji`, que resolve sequências
ZWJ como 👨‍👩‍👧‍👦 e modificadores de tom de pele como 👍🏽) e cada emoji vira uma imagem colada
no lugar:

- **Unicode:** PNG do [Twemoji](https://github.com/jdecked/twemoji) via jsDelivr. O nome
  do arquivo são os codepoints em hex separados por `-` e, quase sempre, **sem** o
  U+FE0F — `2764.png` existe e `2764-fe0f.png` dá 404 — mas há exceções, então as duas
  formas são tentadas nessa ordem.
- **Custom do Discord:** `cdn.discordapp.com/emojis/<id>.png`, tanto para `<:nome:id>`
  quanto para `<a:nome:id>`.
- **Cache** em `data/emoji_cache/` (ignorado pelo git), para não rebaixar o mesmo emoji.
- **Se o download falhar**, o card mostra `:nome:` em texto, nunca um quadrado.

Os downloads acontecem **antes** do render, de forma assíncrona; a thread do Pillow
recebe os bytes prontos e não toca na rede. A quebra de linha e o auto-shrink medem o
emoji como um quadrado do tamanho da fonte (ou como a largura do `:nome:`, quando o
sprite não veio) — sem isso o texto vazaria a margem.

> O `pilmoji` foi avaliado e **não funciona com o Pillow 12**: ele passa uma tupla onde
> o `ImageText` novo espera um objeto de fonte (`AttributeError: 'tuple' object has no
> attribute 'getbbox'`). Daí a implementação própria.

**Card perdido.** A citação é gravada antes do envio, para reservar a mensagem. Se o
envio falhar, a linha fica com `card_posted = 0` e o log registra o motivo; a próxima
reação naquela mensagem tenta publicar de novo, em vez de a mensagem ficar marcada como
usada e nunca virar card.

**A fonte é embarcada em `leviathan/assets/fonts/Inter.ttf`** (Inter, licença SIL OFL,
incluída em `OFL.txt`) e carregada por caminho absoluto relativo ao pacote. Isso é
proposital: o servidor de deploy não tem as fontes da máquina de desenvolvimento, e
depender de fonte do sistema quebraria só em produção. É um arquivo variável, do qual
o código instancia os pesos Regular, Medium e SemiBold.

A data é formatada em português sem depender do locale do servidor, e mostrada no fuso
de Brasília (UTC−3 fixo, já que o Brasil não usa horário de verão desde 2019). Para
mudar, veja `FUSO_EXIBICAO` em [quotes.py](leviathan/cogs/quotes.py).

### Conferir o visual

```bash
uv run python scripts/preview_quote.py
```

Gera oito cards em `preview/` (pasta ignorada pelo git): texto curto, médio, um de
exatamente 400 caracteres, um acima do limite para ver o corte, um sem avatar para ver o
fallback do círculo cinza, e três com emoji — unicode, sequência ZWJ com tom de pele, e
custom do Discord (um que renderiza e um que cai para `:nome:`). Use depois de mexer nas
constantes de layout no topo de `quotes.py`.

## Links úteis (`/pirata` e `/sistema`)

O conteúdo desses dois comandos mora em
[leviathan/data/links.json](leviathan/data/links.json) — nada é hardcoded. Edite o JSON
e rode `/reload links` no Discord: o cog relê o arquivo ao ser recarregado, sem restart
e sem mexer em código. As respostas são públicas, já que a graça é compartilhar no canal.

> **⚠️ Revisar e completar.** Preenchi o JSON com URLs oficiais que conheço, mas
> **elas não foram verificadas uma a uma** e links apodrecem. Confira antes de usar
> para valer. Em particular:
>
> - Os itens de **vídeo tutorial** apontam para uma **busca no YouTube**, de propósito:
>   eu não iria inventar um ID de vídeo específico. Troque cada um pelo vídeo que você
>   realmente recomenda.
> - Os links do FMHY e da megathread do r/Piracy mudam com alguma frequência.
> - Sinta-se à vontade para acrescentar sistemas: basta uma entrada nova em
>   `sistema.opcoes`, e a opção aparece no `/sistema` após o `/reload links`.

Formato do arquivo, por comando:

```jsonc
{
  "pirata": {
    "titulo": "...", "descricao": "...", "cor": "#8e44ad",
    "itens": [ { "nome": "...", "url": "https://...", "descricao": "opcional" } ]
  },
  "sistema": {
    "titulo": "...", "descricao": "...", "cor": "#3498db",
    "opcoes": {
      "arch": { "nome": "Arch Linux", "titulo": "...", "cor": "#1793d1", "itens": [ ... ] }
    }
  }
}
```

`cor` e `descricao` são opcionais; `nome` (da opção) vira o rótulo no menu do
`/sistema`, e a chave (`arch`) é o valor interno. Chaves começando com `_` são
ignoradas, então dá para deixar anotações no arquivo.

**Se o JSON estiver malformado**, o cog falha ao carregar com o caminho exato do
problema no log (`links.json.pirata.itens[0].url precisa começar com http://...`) e
**só ele** fica de fora — o resto do bot sobe normalmente. Num `/reload links` com JSON
quebrado, o discord.py faz rollback e a versão anterior continua no ar.

## Gatilhos de texto

Quando alguém escreve uma palavra cadastrada, o bot responde àquela mensagem (reply,
sem ping) com a URL configurada. Mensagens de bots são ignoradas.

- O casamento é **case-insensitive** e por **palavra inteira** por padrão: `papoi` casa
  em `PAPOI!` e `(papoi)`, mas não em `papoizinho`. A flag `substring` do `/gatilho add`
  troca para casamento em qualquer posição.
- **Cooldown de 30 segundos por canal e por gatilho**, para o bot não virar spam.
  As entradas de cooldown vencidas são descartadas periodicamente, para o dicionário
  em memória não crescer sem fim.
- Se uma mensagem cita vários gatilhos, o bot responde a **um** só.

Gatilhos são sempre **por servidor**: `/gatilho remove` não afeta outro servidor.

O seed (hoje só a palavra `papoi`) vive em `SEED_TRIGGERS`, no topo de
[piadas.py](leviathan/cogs/piadas.py), e é aplicado no primeiro `on_ready` em que o
bot estiver num servidor **que ainda não tem gatilho nenhum**. Ou seja: apagar o
`papoi` de um servidor que tem outros gatilhos não o traz de volta no próximo boot.

## Contadores por reação

`/contador criar 🪱 "o Bruno pisou na minhoca" #placar` faz o bot contar toda vez que
alguém reage com 🪱 — emoji unicode ou custom do servidor, tanto faz. O bot mantém
**uma** mensagem de placar no canal escolhido e a **edita** a cada incremento; se ela
for apagada, o próximo incremento recria e guarda o novo id.

O evento escutado é `on_raw_reaction_add`, e não `on_reaction_add`, para que reações em
mensagens antigas (fora do cache do bot) também contem.

**Normalização de emoji:** dependendo do cliente, o mesmo emoji chega com ou sem o
variation selector (U+FE0F) — `❤️` e `❤` são bytes diferentes. A chave gravada é sempre
a forma normalizada (sem U+FE0F e sem ZWJ sobrando no fim), tanto na criação quanto na
leitura da reação; sem isso o incremento seria descartado em silêncio, sem erro no log.
Emoji custom é identificado por id (`custom:<id>`), então renomear o emoji não quebra o
contador.

**Debounce do placar:** cada reação não edita a mensagem na hora. Ela marca o contador
como sujo e agenda a publicação para daqui a 3 segundos, cancelando e reagendando se
outra reação chegar nesse intervalo. Uma rajada de reações vira **uma** edição, em vez
de uma por pessoa, o que evita bater no rate limit do Discord. As publicações agendadas
são canceladas no `cog_unload`, para `/reload piadas` não deixar task órfã.

**Deduplicação:** a trinca `(mensagem, emoji, pessoa que reagiu)` é a chave primária de
`counter_hits`, então ela só entra uma vez. O bot não escuta remoção de reação de
propósito: remover e reagir de novo esbarra na mesma chave e não conta. Reações do
próprio bot, de outros bots e em mensagens escritas por bots são ignoradas.

### Como validar a deduplicação

Automático:

```bash
uv run pytest -q
```

Os testes ficam em [tests/test_piadas.py](tests/test_piadas.py) e rodam sobre um SQLite
temporário, sem subir o bot. Os que cobrem exatamente esse ponto:

- `test_mesma_reacao_conta_uma_vez_so`
- `test_remover_e_reagir_de_novo_nao_conta_de_novo`
- `test_pessoas_diferentes_na_mesma_mensagem_contam`
- `test_mesma_pessoa_em_mensagens_diferentes_conta`

No servidor, com o bot no ar:

1. `/contador criar 🪱 teste #algum-canal` — o placar aparece com `0`.
2. Reaja com 🪱 em uma mensagem qualquer → o placar vai para `1`.
3. **Remova** sua reação e reaja de novo → o placar **continua** `1`.
4. Peça para outra pessoa reagir na mesma mensagem → vai para `2`.
5. Reaja com 🪱 em outra mensagem → vai para `3`.
6. Apague a mensagem de placar e provoque um incremento → o bot recria o placar.
7. `/contador remover teste` limpa tudo.

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
