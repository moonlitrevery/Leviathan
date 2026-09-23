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
    lastfm.py     # /fm: tocando, recentes, top e compatibilidade
    quemfalou.py  # jogo de adivinhar o autor de mensagens antigas
    alertas.py    # avisos de vídeo, capítulo, episódio e RSS novos
    saude.py      # log de vida de hora em hora
  data/
    links.json    # conteúdo dos comandos de links (versionado, editável)
  assets/
    fonts/        # fonte embarcada, usada pelos cards de citação
deploy/
  leviathan.service       # unit do systemd (alternativa ao Docker)
  leviathan-backup.cron   # agendamento diário do backup
  backup.sh               # backup do banco com o .backup do sqlite3
Dockerfile
docker-compose.yml
scripts/
  preview_quote.py  # gera previews do card sem subir o bot
tests/
  fixtures/       # feed real do YouTube usado nos testes de alertas
  test_piadas.py  # deduplicação dos contadores e casamento dos gatilhos
  test_quotes.py  # segmentação de emoji e medição do card
  test_lastfm.py  # cálculo de compatibilidade musical
  test_quemfalou.py  # filtro de mensagem, silêncio e um palpite por pessoa
  test_alertas.py    # parse de feed, agrupamento de capítulos, backoff e poda
  test_saude.py      # formatação do log de vida e descoberta dos laços
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
| `LASTFM_API_KEY` | não | Chave da [API da Last.fm](https://www.last.fm/api/account/create). Sem ela, só os comandos `/fm` ficam de fora |

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
| `/fm vincular <usuario>` | todos | Liga seu Discord a um perfil da Last.fm |
| `/fm desvincular` | todos | Remove o vínculo |
| `/fm tocando [membro]` | todos | O que está tocando agora, ou a última faixa |
| `/fm recentes [membro]` | todos | Últimas 10 faixas, com horário relativo |
| `/fm top [membro] [tipo] [periodo]` | todos | Top 10 de artistas, músicas ou álbuns |
| `/fm compat <membro1> [membro2]` | todos | Compatibilidade musical entre duas pessoas |
| `/quemfalou config <canal> [...]` | Gerenciar servidor | Configura canal, intervalo, pontos, fonte e duração |
| `/quemfalou excluir <canal>` | Gerenciar servidor | Tira um canal do sorteio |
| `/quemfalou incluir <canal>` | Gerenciar servidor | Devolve um canal ao sorteio |
| `/quemfalou agora` | todos (cooldown 30 min) | Dispara uma rodada na hora |
| `/quemfalou rank` | todos | Placar: pontos, acertos e taxa |
| `/quemfalou pausar` · `retomar` | Gerenciar servidor | Liga e desliga as rodadas automáticas |
| `/alerta youtube <canal> [...]` | Gerenciar servidor | Avisa quando sair vídeo novo num canal do YouTube |
| `/alerta manga <titulo> [...]` | Gerenciar servidor | Avisa quando sair capítulo novo (MangaDex) |
| `/alerta anime <titulo> [...]` | Gerenciar servidor | Avisa quando um episódio for ao ar (AniList) |
| `/alerta rss <url> <nome> [...]` | Gerenciar servidor | Avisa quando sair item novo em qualquer feed |
| `/alerta list` | todos | Assinaturas do servidor, com canal e última checagem |
| `/alerta remover <assinatura>` | Gerenciar servidor | Cancela uma assinatura |
| `/alerta testar <assinatura>` | Gerenciar servidor | Checa na hora e mostra o item mais recente, sem marcar nada |

## Alertas de conteúdo novo (`/alerta`)

Uma assinatura é uma fonte externa que avisa num canal do Discord. São quatro
tipos, e nenhum deles precisa de chave de API.

| Tipo | De onde vem | Como identifica o item |
| --- | --- | --- |
| YouTube | `youtube.com/feeds/videos.xml?channel_id=UC...` | id do vídeo |
| Mangá/manhwa | API do MangaDex | número do capítulo |
| Anime | GraphQL do AniList | número do episódio |
| RSS | qualquer feed RSS/Atom | guid ou link da entrada |

### A regra que mais importa

**Ao cadastrar uma assinatura, tudo que já está no feed é marcado como visto sem
notificar.** O feed do YouTube traz sempre os quinze últimos vídeos; sem essa
carga inicial, assinar um canal despejaria quinze alertas de uma vez no servidor.
Quem faz isso é `separar_novidades(..., notificar=False)`, e o cadastro responde
dizendo quantos itens foram silenciados.

Se a fonte estiver fora do ar bem na hora do cadastro, a assinatura é criada
assim mesmo e fica marcada com `carga_pendente`: a primeira checagem que der
certo faz o papel da carga inicial, também sem avisar ninguém. Sem essa marca, a
assinatura criada com a fonte caída despejaria o feed inteiro no tick seguinte —
justamente o que a regra evita.

### YouTube

O canal pode ser informado como `@handle`, URL ou o id `UC...`. Handle e URL são
resolvidos baixando a página do canal e lendo o **link canônico** (com
`<meta itemprop="identifier">` e `"externalId"` como reserva).

Uma armadilha vale registro: o campo `"channelId"` aparece dezenas de vezes no
HTML da página, e as primeiras ocorrências são de **canais recomendados na barra
lateral** — usar a primeira faria o bot assinar, calado, um canal que ninguém
pediu. Há um teste de regressão para isso em `test_alertas.py`.

As requisições ao `youtube.com` levam os cookies `SOCS` e `CONSENT`, que são a
resposta "já consenti" gravada pelo próprio YouTube. Sem eles, um servidor na
Europa recebe a tela de consentimento de cookies em vez do canal. Se ainda assim
o id não for encontrado, o comando explica o que houve e sugere passar o `UC...`
direto.

Com `ignorar_shorts`, cada vídeo novo leva um `HEAD` em `youtube.com/shorts/<id>`
sem seguir redirect: **200 é short, redirect (303) não é**. Os shorts descartados
mesmo assim entram na lista de vistos — se não entrassem, seriam testados de novo
a cada checagem, para sempre.

### Mangá: um alerta por capítulo, não por upload

O mesmo capítulo existe várias vezes no MangaDex: um upload por idioma e por
grupo de scan. Notificar por upload encheria o canal de avisos repetidos do mesmo
capítulo, então `agrupar_capitulos()` agrupa pelo **número do capítulo** e junta
os idiomas num detalhe só. O link do alerta aponta para o upload mais recente.
Capítulo sem número (oneshot, extra) cai no id do próprio upload.

As chamadas ao MangaDex passam todas por uma fila com intervalo mínimo de 400 ms
entre elas, para respeitar o limite de requisições por IP.

### Anime

A agenda de exibição vem de `Page.airingSchedules` na raiz do GraphQL, e **não**
de `Media.airingSchedule`: o campo dentro de `Media` não aceita `sort`, então só
pela raiz dá para pedir os episódios já exibidos em ordem decrescente.

### Notificação

Embed com título, link, thumbnail quando houver, nome da fonte e cor por tipo.
Mais de três itens novos de uma vez viram **um** embed com a lista, em vez de uma
enxurrada de embeds.

O cargo a mencionar é opcional e por assinatura. Como o bot roda com
`allowed_mentions=none()` globalmente, o ping só funciona porque o envio passa um
`AllowedMentions(roles=[cargo])` explícito — com `everyone` e `users` desligados,
para liberar exatamente aquele cargo e nada mais.

**Nada é marcado como visto sem ter sido entregue.** `notificar()` devolve uma
`Entrega` com os itens que realmente saíram, e só esses entram na lista de
vistos. Se a permissão mudou, o canal foi apagado ou o Discord devolveu 500, o
item continua não-visto, a assinatura conta uma falha (entra no backoff) e a
próxima checagem tenta entregar de novo — em vez de o vídeo sumir calado.

A entrega parcial é tratada item a item: quando os avisos viram vários embeds e o
terceiro falha, os dois primeiros já estão no canal, então só eles são marcados e
o terceiro sai na checagem seguinte.

Canal apagado é o único caso em que o item é marcado sem aviso: aí `get_channel`
devolve `None` com o bot conectado, o log sobe para WARNING e os itens entram
como vistos, senão a assinatura tentaria entregá-los para sempre num canal que
não existe mais. Com o bot ainda desconectado, o `None` é só cache frio e a
entrega é adiada.

### Agendamento e resiliência

Um `tasks.loop` de 5 minutos procura assinaturas vencidas (no máximo 12 por tick).
Cada tipo tem seu intervalo mínimo — YouTube 10 min, MangaDex 15, AniList 10,
RSS 15 — e a próxima checagem ganha até 20% de folga aleatória, para as
assinaturas não convergirem todas para o mesmo minuto.

Uma fonte que falha nunca derruba o laço: a falha é contada na assinatura e a
espera dobra a cada tropeço, até o teto de 6 horas (`calcular_backoff`). A partir
da quinta falha seguida o log sobe para WARNING. Um sucesso zera o contador.

A deduplicação é por `(assinatura, id do item)`, e só os **200 ids mais recentes**
ficam guardados por assinatura — o suficiente para qualquer feed, sem deixar a
tabela crescer para sempre. Cada checagem considera no máximo 100 itens, número
escolhido para caber com folga nesses 200: se uma checagem trouxesse mais itens
do que cabem na memória de vistos, a poda descartaria os mais antigos e eles
voltariam a parecer novidade na checagem seguinte.

## Quem falou? (`/quemfalou`)

De tempos em tempos o bot posta uma mensagem antiga do servidor **sem o autor**, e as
pessoas apostam em quem escreveu usando um seletor de membro. Quem acertar primeiro
leva os pontos.

Configure com `/quemfalou config #canal`. Sem configuração, o jogo não roda.

### Privacidade

**Só entram no sorteio canais que o cargo `@everyone` consegue ver E ler o histórico.**
Isso não é conveniência: o canal do jogo é público, e sortear de um canal restrito
exporia a mensagem para quem nunca teve acesso a ela. O bot também precisa conseguir
ler o canal, e `/quemfalou excluir` tira canais específicos mesmo sendo públicos.

O filtro está em `canais_sorteaveis()` e foi verificado contra canal privado, canal em
que o `@everyone` vê mas não lê o histórico, canal excluído à mão, o próprio canal do
jogo e canal que o bot não lê — todos bloqueados.

### Como a mensagem é escolhida

A fonte é configurável: `citacoes` (a tabela do `/quote`, que já guarda `clean_content`),
`historico` (sorteio direto dos canais) ou `ambos`.

No histórico, a técnica evita paginar anos de mensagens: escolhe um canal elegível,
sorteia um instante entre a criação do canal e 7 dias atrás, converte com
`discord.utils.time_snowflake` e lê 100 mensagens em volta com `history(around=...)`.
São até 5 tentativas antes de desistir.

A mensagem precisa passar por todos os filtros: não é de bot, tem pelo menos 25
caracteres e 4 palavras **descontando os links** (o que resolve "não é só link" de
brinde), não parece comando de bot, não é do canal do jogo, e **o autor ainda está no
servidor** — se saiu, ninguém conseguiria acertar. Uma mensagem que já virou rodada
nunca volta: o `UNIQUE (guild_id, origem_message_id)` garante isso.

### A rodada

O embed traz o texto, o canal de origem e uma data vaga ("março de 2025") — sem autor e
sem link. Abaixo vai um `UserSelect`, e não um `Select` comum, porque o comum tem teto
de 25 opções e o servidor tem mais gente que isso.

Cada pessoa tem **um palpite**, garantido pela chave primária
`(rodada_id, usuario_id)`. O autor da mensagem é barrado antes de gastar o palpite. As
respostas de palpite são efêmeras; o resultado vai no embed público.

Ao encerrar, a mensagem é editada revelando o autor, o link para a original, quem
acertou e quantos palpites errados vieram, e o select fica desabilitado.

**Sobrevive a restart.** O select é um `discord.ui.DynamicItem` com o id da rodada no
`custom_id` (`quemfalou:rodada:42`), então o discord.py reconstrói o componente a partir
da própria mensagem quando alguém interage — nada de estado em memória. Rodadas cujo
prazo venceu durante o restart são encerradas pelo laço de expiração, que roda a cada
30s. Confirmei que `DynamicItem` funciona com `UserSelect` no discord.py 2.7.1:
`_refresh_state` encaminha para o item embrulhado, então `values` chega no callback.

Dois acertos simultâneos são resolvidos por um `UPDATE ... WHERE status = 'ativa'`: só
um altera linha, e o outro recebe "alguém chegou primeiro".

### Agendamento

O laço roda a cada 5 minutos e compara a última rodada com `intervalo_horas` da config,
em vez de ser um `loop(hours=X)` fixo. Assim, mudar o intervalo vale na hora, sem
reiniciar o laço. Uma rodada só nasce se: não há rodada ativa, o jogo não está pausado,
não é horário de silêncio (padrão 2h às 9h de Brasília, e a janela pode virar a
meia-noite) e **alguém humano falou no canal do jogo desde a última rodada** — senão um
servidor parado viraria uma fila de rodadas sem ninguém jogando.

Os dois laços são cancelados no `cog_unload`, então `/reload quemfalou` não deixa task
órfã.

## Last.fm (`/fm`)

Precisa de `LASTFM_API_KEY` no `.env`. **Sem a chave, só este cog fica de fora** — o
`setup` levanta `CogUnavailable` e o boot registra um aviso de uma linha, sem traceback:

```
WARNING leviathan.bot: Cog leviathan.cogs.lastfm não carregado: LASTFM_API_KEY não
está definida no .env, então os comandos /fm ficam fora.
```

Cada pessoa vincula a própria conta com `/fm vincular <usuário>`, que **valida o perfil
via `user.getInfo` antes de gravar** — um nome errado viraria um erro confuso só no
primeiro `/fm tocando`. O vínculo é por pessoa do Discord, não por servidor.

Nos comandos de consulta, `membro` é opcional e o padrão é quem chamou. As respostas
são públicas; só os avisos (conta não vinculada, erro de API) são efêmeros, para não
poluir o canal.

### Compatibilidade musical

A API de tasteometer da Last.fm não existe mais, então `/fm compat` faz a conta aqui:
pega o top 50 artistas de cada pessoa em "sempre" e calcula uma **interseção de
histogramas** — cada artista vira uma fatia do total de plays da pessoa, e a
compatibilidade é a soma das menores fatias entre as duas.

Ponderar por plays evita que um artista ouvido uma vez conte o mesmo que o favorito, e
normalizar pelo total deixa a conta justa entre quem tem 50 mil scrobbles e quem tem
500. Duas listas iguais dão 100%, listas sem interseção dão 0%. Os testes em
[tests/test_lastfm.py](tests/test_lastfm.py) travam essas propriedades.

### Detalhes de API

- **Cache em memória de 30s** por (método, parâmetros): vários comandos seguidos não
  martelam a Last.fm.
- **Timeout de 10s**; erro de rede vira mensagem amigável, não traceback.
- **Perfil privado** (erro 17) e **usuário inexistente** (erro 6) têm mensagem própria,
  a primeira apontando para `last.fm/settings/privacy`.
- A Last.fm devolve uma **imagem placeholder de estrela** quando a faixa não tem capa
  (hash `2a96cbd8b46e442fc41c2b86b821562f`). Nesse caso o embed sai sem thumbnail.

## Sessão HTTP compartilhada

O bot mantém **uma** `aiohttp.ClientSession` em `bot.http_session`, aberta no
`setup_hook` e fechada no `close()`. Todo cog que fala com API externa usa ela — hoje o
`quotes` (avatares e sprites de emoji) e o `lastfm`. Abrir uma sessão por cog
desperdiçaria pool de conexões e espalharia o shutdown por vários lugares.

Cogs novos devem usar `self.bot.http_session`, tratando o caso de ela ser `None`
(antes do `setup_hook`, ou em testes).

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

## Deploy na VM ARM do Oracle Cloud

O alvo é uma instância **Ampere A1 (aarch64)** do Always Free rodando Ubuntu. Vale
para qualquer ARM64 com Linux.

### Dependências em aarch64

Nenhuma dependência precisa compilar nessa arquitetura. Todas as wheels foram
conferidas no PyPI, nas versões que estão no `uv.lock`:

| Pacote | Situação em linux/aarch64 |
| --- | --- |
| aiohttp | wheel `manylinux_2_17_aarch64` |
| pillow | wheel `manylinux_2_28_aarch64` |
| discord.py, aiosqlite, feedparser, emoji, python-dotenv | wheel pura (`py3-none-any`) |
| multidict, yarl, frozenlist, propcache | wheel pura |

Ou seja: **nada de `build-essential`, `python3-dev`, `libjpeg-dev` ou `zlib1g-dev`**.
Se um dia alguma wheel sumir e o Pillow precisar ser compilado, aí sim seriam
necessários `build-essential python3-dev libjpeg-dev zlib1g-dev libfreetype6-dev`.

Duas armadilhas que a imagem já evita:

- **Alpine não serve.** As wheels de aarch64 do Pillow e do aiohttp são
  manylinux, ou seja, glibc. No musl do Alpine a instalação cairia no sdist e
  teria de compilar. A imagem é `python:3.12-slim-bookworm`, que tem
  `linux/arm64/v8` oficial.
- **Python 3.12, não 3.13+.** De 3.13 em diante o discord.py passa a depender do
  `audioop-lts`, que não está no `uv.lock`.

### Com Docker (recomendado)

```bash
sudo apt update && sudo apt install -y docker.io docker-compose-v2 git
sudo usermod -aG docker "$USER" && newgrp docker

git clone https://github.com/moonlitrevery/Leviathan.git /opt/leviathan
cd /opt/leviathan
cp .env.example .env && nano .env      # DISCORD_TOKEN, GUILD_ID, LASTFM_API_KEY

docker compose up -d --build           # o build roda na própria VM, em arm64
docker compose logs -f
```

O `docker-compose.yml` já traz `restart: unless-stopped` (sobe no boot e depois
de qualquer queda), o volume nomeado `leviathan-data` em `/app/data` (banco **e**
cache de emoji), o `.env` montado somente leitura e rotação do log em 5 arquivos
de 10 MB — o driver `json-file` do Docker não rotaciona sozinho e encheria o
disco da VM.

Build multi-arch a partir de uma máquina x86, se preferir não compilar na VM:

```bash
docker buildx build --platform linux/amd64,linux/arm64 -t SEU_USUARIO/leviathan --push .
```

### Sem Docker, com systemd

```bash
sudo apt update && sudo apt install -y git curl sqlite3
sudo useradd --system --create-home --home-dir /home/leviathan --shell /usr/sbin/nologin leviathan

sudo git clone https://github.com/moonlitrevery/Leviathan.git /opt/leviathan
sudo chown -R leviathan:leviathan /opt/leviathan

# uv em /usr/local/bin, que é onde a unit espera encontrá-lo
curl -LsSf https://astral.sh/uv/install.sh | sudo env UV_INSTALL_DIR=/usr/local/bin sh

sudo -u leviathan cp /opt/leviathan/.env.example /opt/leviathan/.env
sudo -u leviathan nano /opt/leviathan/.env

sudo cp /opt/leviathan/deploy/leviathan.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now leviathan
```

A unit usa `Restart=always` com `RestartSec=10`, faz `uv sync --locked` antes de
cada partida (um `git pull` que mudou dependência já entra no restart) e desiste
depois de 5 quedas em 5 minutos, para não martelar a VM em ciclo de erro. O
`uv` guarda cache e Python dentro de `/opt/leviathan`, porque a unit deixa
`/home` somente leitura.

### Firewall do Oracle

**Não precisa abrir porta nenhuma.** O bot só faz conexão de **saída**: gateway do
Discord (WebSocket), API do Discord, YouTube, MangaDex, AniList e Last.fm — tudo
HTTPS/443 iniciado por ele. Não há servidor HTTP, webhook nem healthcheck
escutando, e por isso não há `EXPOSE` no Dockerfile nem portas publicadas no
compose.

Em outras palavras: a Security List / NSG da VCN e o `iptables` da imagem Ubuntu
do Oracle podem continuar como vieram. A única regra de entrada de que você
precisa é a do SSH (22), que já existe. Se o bot não conectar, o problema **não**
é porta de entrada fechada — olhe o token, a saída para a internet e o DNS.

### Como atualizar

```bash
cd /opt/leviathan
git pull

# Docker:
docker compose up -d --build

# systemd (o uv sync roda sozinho no ExecStartPre):
sudo systemctl restart leviathan
```

Migração de banco não precisa de passo manual: o `init_db()` aplica no boot o que
estiver faltando, uma migration por vez. Vale tirar um backup antes de atualizar,
justamente por causa disso.

### Como ver os logs

```bash
# Docker
docker compose logs -f              # acompanhar
docker compose logs --since 1h      # última hora
docker compose logs | grep "vida |" # só o batimento

# systemd
journalctl -u leviathan -f
journalctl -u leviathan --since "1 hour ago"
journalctl -u leviathan -p warning  # só avisos e erros
```

### Log de vida

De hora em hora o cog `saude.py` escreve uma linha dizendo que está tudo de pé:

```
vida | conectado como Leviathan#1234 | 1 guild(s) | latência: 48 ms | no ar há 3h12m | assinaturas: 7 (2 em backoff) | laços: 3/3 rodando
```

Serve para distinguir "o servidor está quieto" de "o bot morreu" — os dois são
silêncio no Discord, mas só um deles some do log. Os laços são descobertos
sozinhos em todos os cogs carregados, então um cog novo com `tasks.loop` entra no
relatório sem ninguém precisar editar o `saude.py`. Quando algo está errado, sai
também uma linha de WARNING por problema:

```
vida | alertas.ciclo: PAROU COM ERRO
```

### Backup

`deploy/backup.sh` usa o **`.backup` do sqlite3**, não `cp`. A diferença importa:
o banco roda em modo WAL, então o arquivo `.db` quente está incompleto — parte das
escritas confirmadas vive no `-wal`. Copiar só o `.db` rende um backup
silenciosamente desatualizado, e copiar os três arquivos em sequência rende uma
combinação que nunca existiu. O `.backup` usa a API de backup online do SQLite e
lê um retrato consistente com o bot escrevendo.

Cada execução verifica o resultado com `PRAGMA integrity_check` antes de aceitá-lo,
só então renomeia o arquivo para o nome definitivo (uma execução interrompida não
deixa um `.db` pela metade parecendo bom), comprime com gzip e apaga o que passou
de 7 dias.

```bash
sudo -u leviathan /opt/leviathan/deploy/backup.sh
DESTINO=/mnt/backups RETENCAO=14 deploy/backup.sh   # dá para mudar

sudo cp deploy/leviathan-backup.cron /etc/cron.d/leviathan-backup
sudo chown root:root /etc/cron.d/leviathan-backup && sudo chmod 644 /etc/cron.d/leviathan-backup
```

Com Docker, o script e o `sqlite3` estão dentro da imagem, e os backups ficam no
mesmo volume do banco:

```bash
docker exec -e DESTINO=/app/data/backups leviathan /app/deploy/backup.sh
```

Restaurar é descomprimir por cima, com o bot parado:

```bash
sudo systemctl stop leviathan          # ou: docker compose stop
gunzip -c backups/leviathan-AAAAMMDD-HHMMSS.db.gz > data/leviathan.db
rm -f data/leviathan.db-wal data/leviathan.db-shm   # sobras do banco antigo
sudo systemctl start leviathan         # ou: docker compose start
```

### Portabilidade do código

O código não assume Windows. Todos os caminhos são `pathlib.Path` montados a
partir de `Path(__file__)`, nunca strings com `\`; o `links.json` é lido com
`encoding="utf-8"` explícito; a fonte dos cards é um arquivo versionado em
`leviathan/assets/fonts/Inter.ttf`, carregado por caminho absoluto, sem depender
de fonte instalada no sistema.

Dois pontos que **são** específicos de plataforma e estão resolvidos:

- O `setup_logging()` faz `reconfigure(encoding="utf-8")` na saída. Isso existe
  por causa do Windows, onde a saída redirecionada cai no code page local e
  embaralha os acentos; no Linux é inofensivo.
- O repositório é desenvolvido no Windows com `core.autocrlf=true`. O
  `.gitattributes` força LF em `*.sh`, `*.service`, `*.cron`, `Dockerfile` e
  `docker-compose.yml`, senão eles chegariam na VM com CRLF e quebrariam — o
  shell reclama de `\r` no shebang e o systemd não entende a unit. O bit de
  execução do `backup.sh` também está registrado no índice do git (`100755`).

O fuso horário é tratado no código com offset fixo de UTC−3 (`FUSO`), sem
depender do `tzdata` da máquina, então a VM pode continuar em UTC.

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
