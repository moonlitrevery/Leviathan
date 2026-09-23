"""Alertas de conteúdo novo: YouTube, mangá/manhwa, anime e RSS genérico.

Cada assinatura é uma fonte externa que avisa num canal do Discord. As quatro
fontes têm em comum o mesmo ciclo: buscar a lista de itens, descartar o que já
foi visto, notificar o resto e marcar tudo como visto. O que muda de uma para
outra é só de onde os itens vêm, então tudo converge cedo para :class:`Item`.

A regra que mais importa está em :func:`separar_novidades`: ao cadastrar uma
assinatura, tudo que já existe no feed é marcado como visto **sem notificar**.
Sem isso, assinar um canal com quinze vídeos no feed despejaria quinze alertas
de uma vez.

Nenhuma fonte deste cog precisa de chave de API: o YouTube é lido pelo RSS
público, o MangaDex e o AniList têm APIs abertas.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlparse

import aiohttp
import discord
import feedparser
from discord import app_commands
from discord.ext import commands, tasks

from leviathan.bot import LeviathanBot
from leviathan.db import Database

log = logging.getLogger(__name__)

# -- tipos de fonte ---------------------------------------------------------

TIPO_YOUTUBE = "youtube"
TIPO_MANGA = "manga"
TIPO_ANIME = "anime"
TIPO_RSS = "rss"

ROTULOS = {
    TIPO_YOUTUBE: "YouTube",
    TIPO_MANGA: "Mangá",
    TIPO_ANIME: "Anime",
    TIPO_RSS: "RSS",
}

CORES = {
    TIPO_YOUTUBE: discord.Color.from_str("#ff0000"),
    TIPO_MANGA: discord.Color.from_str("#ff6740"),
    TIPO_ANIME: discord.Color.from_str("#02a9ff"),
    TIPO_RSS: discord.Color.from_str("#ee802f"),
}

#: Intervalo mínimo entre duas checagens da mesma assinatura.
INTERVALOS = {
    TIPO_YOUTUBE: timedelta(minutes=10),
    TIPO_MANGA: timedelta(minutes=15),
    TIPO_ANIME: timedelta(minutes=10),
    TIPO_RSS: timedelta(minutes=15),
}

#: De quanto em quanto tempo o laço procura assinaturas vencidas.
TICK = timedelta(minutes=5)

#: Teto do backoff: mesmo uma fonte morta há dias volta a ser tentada de 6 em 6h.
BACKOFF_MAX = timedelta(hours=6)

#: A partir daqui as falhas consecutivas viram WARNING no log.
FALHAS_PARA_ALARDE = 5

#: Quantos ids de item ficam guardados por assinatura.
LIMITE_VISTOS = 200

#: Teto de itens considerados por checagem. Precisa ficar CONFORTAVELMENTE
#: abaixo de LIMITE_VISTOS: se uma checagem trouxesse mais itens do que cabem na
#: memória de vistos, a poda jogaria fora os mais antigos e eles voltariam a
#: parecer novidade na checagem seguinte, virando alerta repetido.
LIMITE_ITENS = 100

#: Acima disso, os itens novos viram um embed só com a lista.
MAX_EMBEDS_SEPARADOS = 3

#: Quantas assinaturas no máximo são checadas por tick, para que um tick nunca
#: vire uma rajada de dezenas de requisições.
POR_TICK = 12

#: Espalhamento: a próxima checagem ganha até 20% de folga aleatória, para as
#: assinaturas não convergirem todas para o mesmo minuto.
JITTER = 0.2

IDIOMAS_PADRAO = ("pt-br", "en")

#: Idiomas oferecidos no comando de mangá. O MangaDex usa códigos ISO com
#: variante (pt-br é diferente de pt).
IDIOMA_CHOICES = [
    app_commands.Choice(name="português (BR) e inglês", value="pt-br,en"),
    app_commands.Choice(name="só português (BR)", value="pt-br"),
    app_commands.Choice(name="só inglês", value="en"),
    app_commands.Choice(name="português (BR), inglês e espanhol", value="pt-br,en,es"),
]

# -- HTTP -------------------------------------------------------------------

YOUTUBE_FEED = "https://www.youtube.com/feeds/videos.xml?channel_id={}"
MANGADEX_API = "https://api.mangadex.org"
ANILIST_API = "https://graphql.anilist.co"

#: Alguns servidores europeus recebem a página de consentimento de cookies em
#: vez do canal. Esses dois cookies são a resposta "já consenti" que o próprio
#: YouTube grava, e evitam o desvio sem precisar de sessão logada.
COOKIES_CONSENTIMENTO = {"SOCS": "CAI", "CONSENT": "YES+cb"}

#: A página de canal do YouTube passa de 2 MB e o id só aparece lá pela metade.
TIMEOUT_PAGINA = aiohttp.ClientTimeout(total=25)
TIMEOUT_FEED = aiohttp.ClientTimeout(total=15)

#: O MangaDex pede "algumas requisições por segundo"; uma a cada 400 ms fica
#: bem abaixo do teto mesmo com várias assinaturas vencendo no mesmo tick.
INTERVALO_MANGADEX = 0.4

UC_RE = re.compile(r"^UC[\w-]{22}$")
HANDLE_RE = re.compile(r"^@[\w.\-]{3,30}$")

#: Ordem deliberada: o link canônico e o ``itemprop`` descrevem a página atual.
#: O campo ``"channelId"`` solto NÃO serve — na página de um canal ele aparece
#: dezenas de vezes e a primeira ocorrência costuma ser de um canal recomendado,
#: o que faria o bot assinar o canal errado calado.
PADROES_CHANNEL_ID = (
    re.compile(r'rel="canonical"\s+href="https://www\.youtube\.com/channel/(UC[\w-]{22})"'),
    re.compile(r'<meta\s+itemprop="identifier"\s+content="(UC[\w-]{22})"'),
    re.compile(r'"externalId"\s*:\s*"(UC[\w-]{22})"'),
)

NOME_CANAL_RE = re.compile(r'<meta\s+property="og:title"\s+content="([^"]*)"')

_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE
)


class FonteIndisponivel(Exception):
    """Falha ao consultar uma fonte. Vira mensagem para quem usou o comando."""


# ---------------------------------------------------------------------------
# Lógica pura
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Item:
    """Um item de qualquer fonte, já normalizado.

    ``id`` é a chave de deduplicação e precisa ser estável entre checagens: o id
    do vídeo, o número do capítulo, o número do episódio, o guid do RSS.
    """

    id: str
    titulo: str
    url: str
    thumbnail: str | None = None
    publicado_em: datetime | None = None
    detalhe: str = ""


@dataclass(frozen=True, slots=True)
class Entrega:
    """O que realmente saiu num envio e, se algo deu errado, o motivo.

    Um booleano não bastaria: quando os itens viram vários embeds e o terceiro
    falha, os dois primeiros já chegaram no canal. Quem marca os vistos precisa
    saber exatamente o que foi entregue, senão um item que ninguém viu ficaria
    marcado e nunca mais viraria alerta.
    """

    entregues: tuple[Item, ...] = ()
    erro: str | None = None

    @property
    def ok(self) -> bool:
        return self.erro is None


def calcular_backoff(falhas: int, base: timedelta) -> timedelta:
    """Espera até a próxima tentativa, dobrando a cada falha consecutiva.

    Sem falha nenhuma vale o intervalo normal da fonte. A partir da primeira, o
    intervalo dobra a cada tropeço e para de crescer em :data:`BACKOFF_MAX` —
    uma fonte fora do ar há dias continua sendo tentada de seis em seis horas,
    em vez de ser esquecida.
    """
    if falhas <= 0:
        return base
    # 2**falhas cresce rápido; o min() é aplicado em segundos para não depender
    # de multiplicação de timedelta por número gigante.
    segundos = base.total_seconds() * (2**min(falhas, 20))
    return timedelta(seconds=min(segundos, BACKOFF_MAX.total_seconds()))


def separar_novidades(
    itens: list[Item], vistos: set[str], *, notificar: bool
) -> tuple[list[Item], list[str]]:
    """``(o que notificar, os ids a marcar como vistos)``.

    Com ``notificar=False`` — o caso do cadastro — nada é anunciado e todo o
    feed entra como visto de uma vez. É essa chamada que impede a enxurrada de
    alertas retroativos ao assinar uma fonte com histórico.

    Os itens saem do mais antigo para o mais novo, para que a ordem no Discord
    seja a ordem em que as coisas aconteceram.
    """
    novos = [item for item in itens if item.id not in vistos]
    # A maioria das fontes devolve do mais novo para o mais velho.
    novos.reverse()
    if not notificar:
        return [], [item.id for item in novos]
    return novos, [item.id for item in novos]


def _data_de(entrada: Any) -> datetime | None:
    """Converte o ``struct_time`` do feedparser em datetime UTC."""
    for campo in ("published_parsed", "updated_parsed"):
        marca = entrada.get(campo)
        if marca:
            return datetime(*marca[:6], tzinfo=timezone.utc)
    return None


def _thumbnail_de(entrada: Any) -> str | None:
    """Primeira imagem utilizável da entrada, olhando os lugares usuais."""
    miniaturas = entrada.get("media_thumbnail") or []
    if miniaturas and miniaturas[0].get("url"):
        return miniaturas[0]["url"]
    for conteudo in entrada.get("media_content") or []:
        if conteudo.get("url") and str(conteudo.get("type", "")).startswith("image"):
            return conteudo["url"]
    for link in entrada.get("links") or []:
        if link.get("rel") == "enclosure" and str(link.get("type", "")).startswith("image"):
            return link.get("href")
    return None


def parse_feed(texto: str | bytes) -> tuple[str, list[Item]]:
    """``(título do feed, itens)`` de qualquer RSS/Atom, YouTube incluído.

    O feedparser é síncrono e tolerante a XML torto; quem chama roda em
    ``asyncio.to_thread``. O id do YouTube sai de ``yt_videoid``, que é o id do
    vídeo puro e serve tanto de chave de dedup quanto de argumento para o teste
    de Shorts.
    """
    analisado = feedparser.parse(texto)
    titulo = str(analisado.feed.get("title") or "").strip()

    itens: list[Item] = []
    for entrada in analisado.entries[:LIMITE_ITENS]:
        identificador = (
            entrada.get("yt_videoid") or entrada.get("id") or entrada.get("link")
        )
        if not identificador:
            continue
        itens.append(
            Item(
                id=str(identificador),
                titulo=str(entrada.get("title") or "(sem título)").strip(),
                url=str(entrada.get("link") or ""),
                thumbnail=_thumbnail_de(entrada),
                publicado_em=_data_de(entrada),
            )
        )
    return titulo, itens


def extrair_channel_id(html: str) -> str | None:
    """Acha o ``UC...`` da página de um canal do YouTube.

    Ver :data:`PADROES_CHANNEL_ID` para o motivo de a ordem importar.
    """
    for padrao in PADROES_CHANNEL_ID:
        encontrado = padrao.search(html)
        if encontrado:
            return encontrado.group(1)
    return None


def extrair_nome_canal(html: str) -> str | None:
    encontrado = NOME_CANAL_RE.search(html)
    return encontrado.group(1).strip() if encontrado else None


def interpretar_alvo_youtube(texto: str) -> tuple[str, str]:
    """``("id", "UC...")`` ou ``("pagina", url)`` para o que a pessoa digitou.

    Aceita o id cru, um ``@handle``, uma URL de canal e uma URL de handle. Tudo
    que não é id vira uma URL a ser baixada, porque só a página do canal sabe
    dizer qual é o id.
    """
    texto = texto.strip()
    if UC_RE.match(texto):
        return "id", texto
    if HANDLE_RE.match(texto):
        return "pagina", f"https://www.youtube.com/{texto}"

    if "youtube.com" in texto or "youtu.be" in texto:
        endereco = texto if "://" in texto else f"https://{texto}"
        caminho = urlparse(endereco).path.strip("/")
        partes = caminho.split("/")
        if len(partes) >= 2 and partes[0] == "channel" and UC_RE.match(partes[1]):
            return "id", partes[1]
        return "pagina", endereco

    # Sobrou um handle digitado sem o arroba.
    if re.match(r"^[\w.\-]{3,30}$", texto):
        return "pagina", f"https://www.youtube.com/@{texto}"
    raise FonteIndisponivel(
        "Não reconheci esse canal. Use o @handle, a URL do canal ou o id que começa com UC."
    )


def _numero_do_capitulo(atributos: dict[str, Any]) -> str | None:
    numero = atributos.get("chapter")
    return str(numero).strip() if numero not in (None, "") else None


def agrupar_capitulos(dados: list[dict[str, Any]]) -> list[Item]:
    """Um item por número de capítulo, não por upload.

    O mesmo capítulo costuma existir várias vezes no MangaDex: um upload por
    idioma e por grupo de scan. Notificar por upload encheria o canal de avisos
    repetidos do capítulo 1193, então a chave é o número do capítulo e os
    idiomas de todos os uploads viram um detalhe só.

    Capítulo sem número (oneshot, extra) cai no id do próprio upload, que é o
    melhor que dá para fazer sem inventar agrupamento.
    """
    agrupados: dict[str, dict[str, Any]] = {}
    for capitulo in dados:
        atributos = capitulo.get("attributes") or {}
        numero = _numero_do_capitulo(atributos)
        chave = f"cap:{numero}" if numero else f"id:{capitulo.get('id')}"

        publicado = atributos.get("publishAt")
        quando = _iso_para_data(publicado)
        idioma = atributos.get("translatedLanguage")

        registro = agrupados.get(chave)
        if registro is None:
            agrupados[chave] = {
                "numero": numero,
                "titulo": (atributos.get("title") or "").strip(),
                "id_upload": capitulo.get("id"),
                "quando": quando,
                "idiomas": [idioma] if idioma else [],
            }
            continue

        if idioma and idioma not in registro["idiomas"]:
            registro["idiomas"].append(idioma)
        # Fica o upload mais recente como link do alerta.
        if quando and (registro["quando"] is None or quando > registro["quando"]):
            registro["quando"] = quando
            registro["id_upload"] = capitulo.get("id")
        if not registro["titulo"] and atributos.get("title"):
            registro["titulo"] = str(atributos["title"]).strip()

    itens = []
    for chave, registro in agrupados.items():
        numero = registro["numero"]
        rotulo = f"Capítulo {numero}" if numero else "Capítulo novo"
        if registro["titulo"]:
            rotulo = f"{rotulo} — {registro['titulo']}"
        itens.append(
            Item(
                id=chave,
                titulo=rotulo,
                url=f"https://mangadex.org/chapter/{registro['id_upload']}",
                publicado_em=registro["quando"],
                detalhe=", ".join(sorted(registro["idiomas"])),
            )
        )
    itens.sort(key=lambda item: item.publicado_em or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    return itens


def _iso_para_data(valor: str | None) -> datetime | None:
    if not valor:
        return None
    try:
        return datetime.fromisoformat(valor.replace("Z", "+00:00"))
    except ValueError:
        return None


def episodios_exibidos(
    nodes: list[dict[str, Any]], media: dict[str, Any]
) -> list[Item]:
    """Transforma a agenda de exibição do AniList em itens."""
    titulo_obra = _titulo_anilist(media)
    capa = ((media.get("coverImage") or {}).get("large")) or None
    site = media.get("siteUrl") or ""

    itens = []
    for node in nodes:
        episodio = node.get("episode")
        if episodio is None:
            continue
        itens.append(
            Item(
                id=f"ep:{episodio}",
                titulo=f"Episódio {episodio} — {titulo_obra}",
                url=site,
                thumbnail=capa,
                publicado_em=datetime.fromtimestamp(node["airingAt"], tz=timezone.utc)
                if node.get("airingAt")
                else None,
            )
        )
    return itens


def _titulo_anilist(media: dict[str, Any]) -> str:
    titulos = media.get("title") or {}
    return str(
        titulos.get("romaji") or titulos.get("english") or titulos.get("native") or "?"
    )


def titulo_mangadex(atributos: dict[str, Any]) -> str:
    """Melhor título disponível de um mangá, preferindo pt-br e inglês."""
    titulos = atributos.get("title") or {}
    for idioma in ("pt-br", "pt", "en"):
        if titulos.get(idioma):
            return str(titulos[idioma])
    if titulos:
        return str(next(iter(titulos.values())))
    for alternativo in atributos.get("altTitles") or []:
        for valor in alternativo.values():
            return str(valor)
    return "(sem título)"


def _cortar(texto: str, limite: int) -> str:
    texto = " ".join(texto.split())
    return texto if len(texto) <= limite else texto[: limite - 1] + "…"


# ---------------------------------------------------------------------------
# Banco
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Assinatura:
    id: int
    guild_id: int
    tipo: str
    externo_id: str
    nome: str
    url: str
    channel_id: int
    cargo_id: int | None
    opcoes: dict[str, Any] = field(default_factory=dict)
    ultima_checagem: datetime | None = None
    proxima_checagem: datetime | None = None
    falhas: int = 0

    @property
    def rotulo(self) -> str:
        return ROTULOS.get(self.tipo, self.tipo)


def _assinatura_from_row(row) -> Assinatura:
    try:
        opcoes = json.loads(row["opcoes"] or "{}")
    except json.JSONDecodeError:
        opcoes = {}
    return Assinatura(
        id=row["id"],
        guild_id=row["guild_id"],
        tipo=row["tipo"],
        externo_id=row["externo_id"],
        nome=row["nome"],
        url=row["url"],
        channel_id=row["channel_id"],
        cargo_id=row["cargo_id"],
        opcoes=opcoes if isinstance(opcoes, dict) else {},
        ultima_checagem=_iso_para_data(row["ultima_checagem"]),
        proxima_checagem=_iso_para_data(row["proxima_checagem"]),
        falhas=int(row["falhas"]),
    )


async def criar_assinatura(
    db: Database,
    *,
    guild_id: int,
    tipo: str,
    externo_id: str,
    nome: str,
    url: str,
    channel_id: int,
    cargo_id: int | None,
    opcoes: dict[str, Any] | None = None,
) -> Assinatura | None:
    """Cria a assinatura. ``None`` se essa fonte já estava assinada na guild."""
    afetadas = await db.execute(
        "INSERT OR IGNORE INTO alertas_assinaturas"
        " (guild_id, tipo, externo_id, nome, url, channel_id, cargo_id, opcoes)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            guild_id,
            tipo,
            externo_id,
            nome,
            url,
            channel_id,
            cargo_id,
            json.dumps(opcoes or {}),
        ),
    )
    if not afetadas:
        return None
    return await get_assinatura_por_fonte(db, guild_id, tipo, externo_id)


async def get_assinatura(db: Database, assinatura_id: int) -> Assinatura | None:
    row = await db.fetchone(
        "SELECT * FROM alertas_assinaturas WHERE id = ?", (assinatura_id,)
    )
    return _assinatura_from_row(row) if row else None


async def get_assinatura_por_fonte(
    db: Database, guild_id: int, tipo: str, externo_id: str
) -> Assinatura | None:
    row = await db.fetchone(
        "SELECT * FROM alertas_assinaturas"
        " WHERE guild_id = ? AND tipo = ? AND externo_id = ?",
        (guild_id, tipo, externo_id),
    )
    return _assinatura_from_row(row) if row else None


async def listar_assinaturas(db: Database, guild_id: int) -> list[Assinatura]:
    rows = await db.fetchall(
        "SELECT * FROM alertas_assinaturas WHERE guild_id = ?"
        " ORDER BY tipo ASC, nome COLLATE NOCASE ASC",
        (guild_id,),
    )
    return [_assinatura_from_row(row) for row in rows]


async def assinaturas_vencidas(db: Database, limite: int) -> list[Assinatura]:
    rows = await db.fetchall(
        "SELECT * FROM alertas_assinaturas WHERE proxima_checagem <= ?"
        " ORDER BY proxima_checagem ASC LIMIT ?",
        (datetime.now(timezone.utc).isoformat(), limite),
    )
    return [_assinatura_from_row(row) for row in rows]


async def remover_assinatura(db: Database, assinatura_id: int) -> bool:
    afetadas = await db.execute(
        "DELETE FROM alertas_assinaturas WHERE id = ?", (assinatura_id,)
    )
    return afetadas > 0


async def ids_vistos(db: Database, assinatura_id: int) -> set[str]:
    rows = await db.fetchall(
        "SELECT item_id FROM alertas_vistos WHERE assinatura_id = ?", (assinatura_id,)
    )
    return {row["item_id"] for row in rows}


async def marcar_vistos(db: Database, assinatura_id: int, item_ids: list[str]) -> None:
    """Grava os ids e poda a tabela, mantendo só os mais recentes."""
    if not item_ids:
        return
    await db.executemany(
        "INSERT OR IGNORE INTO alertas_vistos (assinatura_id, item_id) VALUES (?, ?)",
        [(assinatura_id, item_id) for item_id in item_ids],
    )
    await podar_vistos(db, assinatura_id)


async def podar_vistos(
    db: Database, assinatura_id: int, limite: int = LIMITE_VISTOS
) -> int:
    """Apaga tudo além dos ``limite`` ids mais recentes. Devolve quantos saíram.

    O ``rowid`` é a ordem de inserção, que é o que interessa: o mais recente é o
    último a entrar, mesmo quando a fonte não tem data confiável.
    """
    return await db.execute(
        "DELETE FROM alertas_vistos WHERE assinatura_id = ? AND rowid NOT IN ("
        "  SELECT rowid FROM alertas_vistos WHERE assinatura_id = ?"
        "  ORDER BY rowid DESC LIMIT ?"
        ")",
        (assinatura_id, assinatura_id, limite),
    )


async def atualizar_opcoes(
    db: Database, assinatura_id: int, opcoes: dict[str, Any]
) -> None:
    await db.execute(
        "UPDATE alertas_assinaturas SET opcoes = ? WHERE id = ?",
        (json.dumps(opcoes), assinatura_id),
    )


async def registrar_checagem(
    db: Database, assinatura_id: int, *, falhou: bool, proxima: datetime
) -> None:
    """Atualiza contador de falhas e quando esta assinatura será vista de novo."""
    if falhou:
        await db.execute(
            "UPDATE alertas_assinaturas SET falhas = falhas + 1,"
            " ultima_checagem = ?, proxima_checagem = ? WHERE id = ?",
            (datetime.now(timezone.utc).isoformat(), proxima.isoformat(), assinatura_id),
        )
        return
    await db.execute(
        "UPDATE alertas_assinaturas SET falhas = 0,"
        " ultima_checagem = ?, proxima_checagem = ? WHERE id = ?",
        (datetime.now(timezone.utc).isoformat(), proxima.isoformat(), assinatura_id),
    )


# ---------------------------------------------------------------------------
# Consultas às APIs
# ---------------------------------------------------------------------------

BUSCA_ANIME = """
query ($busca: String) {
  Page(page: 1, perPage: 15) {
    media(search: $busca, type: ANIME, sort: SEARCH_MATCH) {
      id
      title { romaji english native }
      format
      status
      startDate { year }
      siteUrl
      coverImage { large }
    }
  }
}
"""

#: A agenda vem da raiz ``Page.airingSchedules``, e não de ``Media.airingSchedule``:
#: o campo dentro de Media não aceita ``sort``, então só de lá dá para pedir os
#: episódios já exibidos em ordem decrescente.
AGENDA_ANIME = """
query ($id: Int, $ate: Int) {
  Media(id: $id, type: ANIME) {
    id
    title { romaji english native }
    siteUrl
    coverImage { large }
  }
  Page(page: 1, perPage: 15) {
    airingSchedules(mediaId: $id, airingAt_lesser: $ate, sort: TIME_DESC) {
      episode
      airingAt
    }
  }
}
"""


# ---------------------------------------------------------------------------
# Cog
# ---------------------------------------------------------------------------


class Alertas(commands.Cog):
    """Assinaturas de fontes externas e o laço que as checa."""

    alerta = app_commands.Group(
        name="alerta",
        description="Avisos de conteúdo novo: YouTube, mangá, anime e RSS.",
        guild_only=True,
    )

    def __init__(self, bot: LeviathanBot) -> None:
        self.bot = bot
        self.db = bot.db
        # O MangaDex limita por IP, então as chamadas deste cog passam todas por
        # aqui, em fila, com um intervalo mínimo entre elas.
        self._trava_mangadex = asyncio.Lock()
        self._ultima_mangadex = float("-inf")

    async def cog_load(self) -> None:
        self.ciclo.start()

    async def cog_unload(self) -> None:
        self.ciclo.cancel()

    # -- HTTP --------------------------------------------------------------

    @property
    def sessao(self) -> aiohttp.ClientSession:
        sessao = self.bot.http_session
        if sessao is None or sessao.closed:
            raise FonteIndisponivel("O bot está sem sessão HTTP no momento.")
        return sessao

    async def baixar_texto(self, url: str, **kwargs: Any) -> str:
        try:
            async with self.sessao.get(url, **kwargs) as resposta:
                if resposta.status != 200:
                    raise FonteIndisponivel(
                        f"A fonte respondeu HTTP {resposta.status}."
                    )
                return await resposta.text()
        except aiohttp.ClientError as exc:
            raise FonteIndisponivel(f"Não consegui acessar a fonte: {exc}") from exc
        except asyncio.TimeoutError as exc:
            raise FonteIndisponivel("A fonte demorou demais para responder.") from exc

    async def mangadex(self, caminho: str, params: Any) -> dict[str, Any]:
        """GET no MangaDex, em fila e com intervalo mínimo entre chamadas."""
        async with self._trava_mangadex:
            espera = INTERVALO_MANGADEX - (time.monotonic() - self._ultima_mangadex)
            if espera > 0:
                await asyncio.sleep(espera)
            try:
                async with self.sessao.get(
                    f"{MANGADEX_API}{caminho}", params=params, timeout=TIMEOUT_FEED
                ) as resposta:
                    if resposta.status == 429:
                        raise FonteIndisponivel(
                            "O MangaDex está limitando as requisições. Tente em instantes."
                        )
                    if resposta.status != 200:
                        raise FonteIndisponivel(
                            f"O MangaDex respondeu HTTP {resposta.status}."
                        )
                    return await resposta.json()
            except aiohttp.ClientError as exc:
                raise FonteIndisponivel(f"Não consegui falar com o MangaDex: {exc}") from exc
            except asyncio.TimeoutError as exc:
                raise FonteIndisponivel("O MangaDex demorou demais para responder.") from exc
            finally:
                self._ultima_mangadex = time.monotonic()

    async def anilist(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        try:
            async with self.sessao.post(
                ANILIST_API,
                json={"query": query, "variables": variables},
                timeout=TIMEOUT_FEED,
            ) as resposta:
                if resposta.status == 429:
                    raise FonteIndisponivel(
                        "O AniList está limitando as requisições. Tente em instantes."
                    )
                corpo = await resposta.json(content_type=None)
        except aiohttp.ClientError as exc:
            raise FonteIndisponivel(f"Não consegui falar com o AniList: {exc}") from exc
        except asyncio.TimeoutError as exc:
            raise FonteIndisponivel("O AniList demorou demais para responder.") from exc
        except ValueError as exc:
            raise FonteIndisponivel("O AniList devolveu uma resposta ilegível.") from exc

        if not isinstance(corpo, dict) or corpo.get("errors"):
            detalhe = ""
            if isinstance(corpo, dict) and corpo.get("errors"):
                detalhe = str(corpo["errors"][0].get("message", ""))
            raise FonteIndisponivel(f"O AniList recusou a consulta. {detalhe}".strip())
        return corpo.get("data") or {}

    # -- resolução de fontes ----------------------------------------------

    async def resolver_youtube(self, alvo: str) -> tuple[str, str]:
        """``(channel_id, nome)`` a partir de handle, URL ou id."""
        tipo, valor = interpretar_alvo_youtube(alvo)
        if tipo == "id":
            titulo, _ = await self.ler_feed(YOUTUBE_FEED.format(valor))
            if not titulo:
                raise FonteIndisponivel(
                    "Esse id de canal não tem feed no YouTube. Confira se está certo."
                )
            return valor, titulo

        html = await self.baixar_texto(
            valor, cookies=COOKIES_CONSENTIMENTO, timeout=TIMEOUT_PAGINA
        )
        channel_id = extrair_channel_id(html)
        if channel_id is None:
            raise FonteIndisponivel(
                "Não achei o id do canal nessa página. O YouTube pode ter devolvido "
                "a tela de consentimento de cookies ou o canal não existe — tente "
                "passar o id que começa com UC, visível em Sobre > Compartilhar canal."
            )
        nome = extrair_nome_canal(html)
        if not nome:
            nome, _ = await self.ler_feed(YOUTUBE_FEED.format(channel_id))
        return channel_id, nome or channel_id

    async def ler_feed(self, url: str) -> tuple[str, list[Item]]:
        """Baixa e interpreta um feed. O parse sai da thread do bot."""
        texto = await self.baixar_texto(url, timeout=TIMEOUT_FEED)
        return await asyncio.to_thread(parse_feed, texto)

    async def buscar_mangas(self, titulo: str) -> list[dict[str, Any]]:
        dados = await self.mangadex(
            "/manga",
            [("title", titulo), ("limit", "15"), ("order[relevance]", "desc")],
        )
        return dados.get("data") or []

    async def buscar_animes(self, titulo: str) -> list[dict[str, Any]]:
        dados = await self.anilist(BUSCA_ANIME, {"busca": titulo})
        return ((dados.get("Page") or {}).get("media")) or []

    async def e_short(self, video_id: str) -> bool:
        """Se o vídeo é um Short.

        O YouTube responde 200 em ``/shorts/<id>`` quando o vídeo é mesmo um
        short, e um redirect para ``/watch?v=`` quando não é. É um HEAD só, sem
        baixar a página e sem seguir o redirect.
        """
        try:
            async with self.sessao.head(
                f"https://www.youtube.com/shorts/{video_id}",
                allow_redirects=False,
                cookies=COOKIES_CONSENTIMENTO,
                timeout=TIMEOUT_FEED,
            ) as resposta:
                return resposta.status == 200
        except (aiohttp.ClientError, asyncio.TimeoutError):
            # Na dúvida, não engole o vídeo: melhor um short a mais do que um
            # vídeo normal a menos.
            log.debug("Não consegui testar se %s é short", video_id)
            return False

    # -- coleta -------------------------------------------------------------

    async def coletar(self, assinatura: Assinatura) -> list[Item]:
        """Itens atuais da fonte, do mais novo para o mais antigo."""
        if assinatura.tipo == TIPO_YOUTUBE:
            _, itens = await self.ler_feed(YOUTUBE_FEED.format(assinatura.externo_id))
            return itens
        if assinatura.tipo == TIPO_RSS:
            _, itens = await self.ler_feed(assinatura.externo_id)
            return itens
        if assinatura.tipo == TIPO_MANGA:
            idiomas = assinatura.opcoes.get("idiomas") or list(IDIOMAS_PADRAO)
            params = [("limit", "50"), ("order[publishAt]", "desc")]
            params += [("translatedLanguage[]", idioma) for idioma in idiomas]
            dados = await self.mangadex(f"/manga/{assinatura.externo_id}/feed", params)
            return agrupar_capitulos(dados.get("data") or [])
        if assinatura.tipo == TIPO_ANIME:
            agora = int(time.time())
            dados = await self.anilist(
                AGENDA_ANIME, {"id": int(assinatura.externo_id), "ate": agora}
            )
            media = dados.get("Media") or {}
            nodes = ((dados.get("Page") or {}).get("airingSchedules")) or []
            return episodios_exibidos(nodes, media)
        raise FonteIndisponivel(f"Tipo de assinatura desconhecido: {assinatura.tipo}")

    # -- laço ---------------------------------------------------------------

    @tasks.loop(seconds=TICK.total_seconds())
    async def ciclo(self) -> None:
        """Checa as assinaturas vencidas. Uma fonte quebrada não derruba o laço."""
        try:
            vencidas = await assinaturas_vencidas(self.db, POR_TICK)
        except Exception:
            log.exception("Falha ao listar assinaturas vencidas")
            return

        for assinatura in vencidas:
            try:
                await self.processar(assinatura)
            except Exception:
                # processar() já trata FonteIndisponivel; aqui é rede de segurança
                # para qualquer coisa inesperada em uma assinatura só.
                log.exception("Falha inesperada na assinatura %d", assinatura.id)

    @ciclo.before_loop
    async def _antes_do_ciclo(self) -> None:
        await self.bot.wait_until_ready()

    def proxima_checagem(self, assinatura: Assinatura, *, falhas: int) -> datetime:
        """Quando esta assinatura deve ser vista de novo, já com espalhamento."""
        base = INTERVALOS.get(assinatura.tipo, timedelta(minutes=15))
        espera = calcular_backoff(falhas, base)
        folga = espera.total_seconds() * JITTER * random.random()
        return datetime.now(timezone.utc) + espera + timedelta(seconds=folga)

    async def processar(self, assinatura: Assinatura) -> list[Item]:
        """Checa uma assinatura, notifica o que é novo e atualiza o banco."""
        try:
            itens = await self.coletar(assinatura)
        except FonteIndisponivel as exc:
            falhas = assinatura.falhas + 1
            if falhas >= FALHAS_PARA_ALARDE:
                log.warning(
                    "A assinatura %d (%s: %s) falhou %d vezes seguidas: %s",
                    assinatura.id, assinatura.tipo, assinatura.nome, falhas, exc,
                )
            else:
                log.info(
                    "Falha %d na assinatura %d (%s): %s",
                    falhas, assinatura.id, assinatura.nome, exc,
                )
            await registrar_checagem(
                self.db,
                assinatura.id,
                falhou=True,
                proxima=self.proxima_checagem(assinatura, falhas=falhas),
            )
            return []

        # Se o cadastro não conseguiu fazer a carga inicial (fonte fora do ar
        # naquele momento), esta primeira checagem bem-sucedida é que faz o papel
        # dela: marca tudo e não avisa nada. Sem isso, uma assinatura criada com
        # a fonte caída despejaria o feed inteiro no canal no tick seguinte.
        pendente = bool(assinatura.opcoes.get("carga_pendente"))
        vistos = await ids_vistos(self.db, assinatura.id)
        novos, marcar = separar_novidades(itens, vistos, notificar=not pendente)

        if pendente:
            restantes = {
                chave: valor
                for chave, valor in assinatura.opcoes.items()
                if chave != "carga_pendente"
            }
            await atualizar_opcoes(self.db, assinatura.id, restantes)
            log.info(
                "Carga inicial atrasada da assinatura %d concluída: %d item(ns)"
                " marcados sem aviso",
                assinatura.id, len(marcar),
            )

        # Itens que não passam por entrega nenhuma: os que a carga pendente
        # silencia e os shorts descartados. Podem ser marcados direto, porque não
        # existe aviso para dar errado.
        sem_entrega = {item_id for item_id in marcar} - {item.id for item in novos}

        if assinatura.tipo == TIPO_YOUTUBE and assinatura.opcoes.get("ignorar_shorts"):
            # O short descartado é marcado assim mesmo: se não fosse, seria
            # testado de novo a cada checagem, para sempre.
            filtrados = []
            for item in novos:
                if await self.e_short(item.id):
                    sem_entrega.add(item.id)
                else:
                    filtrados.append(item)
            novos = filtrados

        entrega = Entrega()
        if novos:
            entrega = await self.notificar(assinatura, novos)
            if not entrega.ok:
                log.warning(
                    "Falha ao entregar %d de %d alerta(s) da assinatura %d (%s): %s",
                    len(novos) - len(entrega.entregues), len(novos),
                    assinatura.id, assinatura.nome, entrega.erro,
                )

        # O item que não chegou ao canal continua não-visto: é o que garante que
        # a próxima checagem tente entregá-lo de novo, em vez de sumir calado.
        entregues = {item.id for item in entrega.entregues}
        a_marcar = [
            item_id
            for item_id in marcar
            if item_id in sem_entrega or item_id in entregues
        ]
        await marcar_vistos(self.db, assinatura.id, a_marcar)

        # A entrega falha entra no backoff da assinatura como qualquer outra
        # falha: não adianta insistir de cinco em cinco minutos num canal sem
        # permissão.
        falhas = assinatura.falhas + 1 if not entrega.ok else 0
        await registrar_checagem(
            self.db,
            assinatura.id,
            falhou=not entrega.ok,
            proxima=self.proxima_checagem(assinatura, falhas=falhas),
        )
        return list(entrega.entregues)

    async def primeira_carga(self, assinatura: Assinatura) -> int:
        """Marca tudo que já existe como visto, sem notificar nada.

        É o que impede que assinar uma fonte com histórico jogue o feed inteiro
        no canal. Se a fonte estiver fora do ar bem na hora do cadastro, a
        assinatura fica registrada e a primeira checagem do laço resolve.
        """
        try:
            itens = await self.coletar(assinatura)
        except FonteIndisponivel:
            # A assinatura continua válida, mas a próxima checagem precisa saber
            # que ainda deve a carga inicial.
            log.info(
                "Assinatura %d cadastrada sem carga inicial: a fonte não respondeu",
                assinatura.id,
            )
            await atualizar_opcoes(
                self.db, assinatura.id, {**assinatura.opcoes, "carga_pendente": True}
            )
            await registrar_checagem(
                self.db,
                assinatura.id,
                falhou=True,
                proxima=self.proxima_checagem(assinatura, falhas=1),
            )
            return 0
        _, marcar = separar_novidades(itens, set(), notificar=False)
        await marcar_vistos(self.db, assinatura.id, marcar)
        await registrar_checagem(
            self.db,
            assinatura.id,
            falhou=False,
            proxima=self.proxima_checagem(assinatura, falhas=0),
        )
        return len(marcar)

    # -- notificação --------------------------------------------------------

    def embed_do_item(self, assinatura: Assinatura, item: Item) -> discord.Embed:
        embed = discord.Embed(
            title=_cortar(item.titulo, 256),
            url=item.url or None,
            color=CORES.get(assinatura.tipo, discord.Color.blurple()),
            timestamp=item.publicado_em,
        )
        embed.set_author(name=_cortar(f"{assinatura.rotulo} · {assinatura.nome}", 256))
        if item.thumbnail:
            embed.set_image(url=item.thumbnail)
        if item.detalhe:
            embed.add_field(name="Idiomas", value=_cortar(item.detalhe, 1024))
        return embed

    def embed_do_lote(self, assinatura: Assinatura, itens: list[Item]) -> discord.Embed:
        """Vários itens de uma vez viram um embed só, com a lista."""
        linhas = []
        for item in itens[:15]:
            titulo = _cortar(item.titulo, 90)
            linhas.append(f"• [{titulo}]({item.url})" if item.url else f"• {titulo}")
        if len(itens) > 15:
            linhas.append(f"… e mais {len(itens) - 15}.")

        embed = discord.Embed(
            title=f"{len(itens)} novidades em {_cortar(assinatura.nome, 200)}",
            description="\n".join(linhas),
            color=CORES.get(assinatura.tipo, discord.Color.blurple()),
            timestamp=datetime.now(timezone.utc),
        )
        embed.set_author(name=assinatura.rotulo)
        capa = next((item.thumbnail for item in reversed(itens) if item.thumbnail), None)
        if capa:
            embed.set_thumbnail(url=capa)
        return embed

    def bot_conectado(self) -> bool:
        """Se dá para confiar no cache do bot para resolver um canal.

        Enquanto o bot não terminou de conectar, ``get_channel`` devolve ``None``
        para canais que existem. Sem essa distinção, um alerta seria descartado
        como "canal apagado" só porque chegou cedo demais.
        """
        return self.bot.is_ready() and not self.bot.is_closed()

    def montar_envios(
        self, assinatura: Assinatura, itens: list[Item]
    ) -> list[tuple[discord.Embed, list[Item]]]:
        """Cada embed a enviar, junto dos itens que ele cobre.

        O par existe por causa da entrega parcial: se um envio falhar no meio, é
        essa correspondência que diz quais itens já chegaram ao canal.
        """
        if len(itens) > MAX_EMBEDS_SEPARADOS:
            return [(self.embed_do_lote(assinatura, itens), list(itens))]
        return [(self.embed_do_item(assinatura, item), [item]) for item in itens]

    async def notificar(self, assinatura: Assinatura, itens: list[Item]) -> Entrega:
        """Envia os alertas e conta o que de fato saiu.

        Nunca engole a falha: quem chama precisa saber o que não foi entregue
        para não marcar como visto um item que ninguém chegou a ver.
        """
        canal = self.bot.get_channel(assinatura.channel_id)
        if not isinstance(canal, discord.abc.Messageable):
            if not self.bot_conectado():
                # Cache ainda frio: é situação passageira, tenta de novo depois.
                return Entrega(
                    erro="o bot ainda não está conectado para resolver o canal"
                )
            # Canal apagado de verdade. Os itens vão como vistos de propósito:
            # sem isso a assinatura tentaria entregá-los para sempre, a cada
            # checagem, num canal que não existe mais.
            log.warning(
                "Canal %d da assinatura %d (%s) não existe mais; %d item(ns)"
                " marcados como vistos sem aviso",
                assinatura.channel_id, assinatura.id, assinatura.nome, len(itens),
            )
            return Entrega(entregues=tuple(itens))

        conteudo = None
        # O bot usa allowed_mentions=none() globalmente, então o ping só sai se
        # este send liberar explicitamente o cargo.
        permitidas = discord.AllowedMentions.none()
        if assinatura.cargo_id:
            conteudo = f"<@&{assinatura.cargo_id}>"
            # Só este cargo: everyone e users ficam desligados de propósito,
            # senão o payload voltaria a permitir o que a política global do bot
            # bloqueia.
            permitidas = discord.AllowedMentions(
                everyone=False,
                users=False,
                replied_user=False,
                roles=[discord.Object(id=assinatura.cargo_id)],
            )

        entregues: list[Item] = []
        for posicao, (embed, cobertos) in enumerate(
            self.montar_envios(assinatura, itens)
        ):
            try:
                await canal.send(
                    content=conteudo if posicao == 0 else None,
                    embed=embed,
                    allowed_mentions=permitidas if posicao == 0 else discord.AllowedMentions.none(),
                )
            except discord.Forbidden:
                return Entrega(
                    entregues=tuple(entregues),
                    erro=f"sem permissão para postar no canal {assinatura.channel_id}",
                )
            except discord.HTTPException as exc:
                return Entrega(
                    entregues=tuple(entregues),
                    erro=f"o Discord recusou o envio: {exc}",
                )
            entregues.extend(cobertos)
        return Entrega(entregues=tuple(entregues))

    # -- comandos -----------------------------------------------------------

    async def _destino(
        self, interaction: discord.Interaction, canal: discord.TextChannel | None
    ) -> discord.TextChannel | None:
        """Canal escolhido ou o canal do comando, já com permissão conferida."""
        alvo = canal or interaction.channel
        if not isinstance(alvo, discord.TextChannel):
            await interaction.followup.send(
                "Escolha um canal de texto para receber os alertas.", ephemeral=True
            )
            return None
        assert alvo.guild is not None
        permissoes = alvo.permissions_for(alvo.guild.me)
        if not (permissoes.send_messages and permissoes.embed_links):
            await interaction.followup.send(
                f"Não consigo postar em {alvo.mention}: preciso de 'Enviar mensagens'"
                " e 'Inserir links'.",
                ephemeral=True,
            )
            return None
        return alvo

    async def _cadastrar(
        self,
        interaction: discord.Interaction,
        *,
        tipo: str,
        externo_id: str,
        nome: str,
        url: str,
        canal: discord.TextChannel,
        cargo: discord.Role | None,
        opcoes: dict[str, Any] | None = None,
    ) -> None:
        assert interaction.guild_id is not None
        assinatura = await criar_assinatura(
            self.db,
            guild_id=interaction.guild_id,
            tipo=tipo,
            externo_id=externo_id,
            nome=nome,
            url=url,
            channel_id=canal.id,
            cargo_id=cargo.id if cargo else None,
            opcoes=opcoes,
        )
        if assinatura is None:
            await interaction.followup.send(
                f"**{nome}** já está assinado neste servidor. Use `/alerta list`.",
                ephemeral=True,
            )
            return

        marcados = await self.primeira_carga(assinatura)

        embed = discord.Embed(
            title="Assinatura criada",
            description=f"**{_cortar(nome, 200)}**",
            color=CORES.get(tipo, discord.Color.blurple()),
            url=url or None,
        )
        embed.add_field(name="Tipo", value=ROTULOS.get(tipo, tipo), inline=True)
        embed.add_field(name="Canal", value=canal.mention, inline=True)
        embed.add_field(
            name="Cargo", value=cargo.mention if cargo else "nenhum", inline=True
        )
        if opcoes:
            legiveis = []
            if opcoes.get("ignorar_shorts"):
                legiveis.append("ignorando Shorts")
            if opcoes.get("idiomas"):
                legiveis.append("idiomas: " + ", ".join(opcoes["idiomas"]))
            if legiveis:
                embed.add_field(name="Opções", value="; ".join(legiveis), inline=False)
        embed.set_footer(
            text=f"{marcados} item(ns) já existentes foram marcados como vistos."
            " Só o que sair daqui para frente vira alerta."
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

    @alerta.command(name="youtube", description="Avisa quando sair vídeo novo num canal.")
    @app_commands.describe(
        canal="@handle, URL do canal ou o id que começa com UC",
        canal_discord="Onde avisar (padrão: este canal)",
        cargo="Cargo a mencionar no aviso",
        ignorar_shorts="Não avisar sobre Shorts",
    )
    @app_commands.checks.has_permissions(manage_guild=True)
    async def cmd_youtube(
        self,
        interaction: discord.Interaction,
        canal: str,
        canal_discord: discord.TextChannel | None = None,
        cargo: discord.Role | None = None,
        ignorar_shorts: bool = False,
    ) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        destino = await self._destino(interaction, canal_discord)
        if destino is None:
            return
        try:
            channel_id, nome = await self.resolver_youtube(canal)
        except FonteIndisponivel as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return

        await self._cadastrar(
            interaction,
            tipo=TIPO_YOUTUBE,
            externo_id=channel_id,
            nome=nome,
            url=f"https://www.youtube.com/channel/{channel_id}",
            canal=destino,
            cargo=cargo,
            opcoes={"ignorar_shorts": bool(ignorar_shorts)},
        )

    @alerta.command(name="manga", description="Avisa quando sair capítulo novo.")
    @app_commands.describe(
        titulo="Título do mangá ou manhwa (escolha uma sugestão)",
        idiomas="Idiomas das traduções (padrão: pt-br e inglês)",
        canal_discord="Onde avisar (padrão: este canal)",
        cargo="Cargo a mencionar no aviso",
    )
    @app_commands.choices(idiomas=IDIOMA_CHOICES)
    @app_commands.checks.has_permissions(manage_guild=True)
    async def cmd_manga(
        self,
        interaction: discord.Interaction,
        titulo: str,
        idiomas: str | None = None,
        canal_discord: discord.TextChannel | None = None,
        cargo: discord.Role | None = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        destino = await self._destino(interaction, canal_discord)
        if destino is None:
            return

        escolhidos = [
            idioma.strip()
            for idioma in (idiomas or ",".join(IDIOMAS_PADRAO)).split(",")
            if idioma.strip()
        ]
        try:
            manga = await self._achar_manga(titulo)
        except FonteIndisponivel as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        if manga is None:
            await interaction.followup.send(
                f"Não achei nenhum mangá com o título “{_cortar(titulo, 100)}”.",
                ephemeral=True,
            )
            return

        await self._cadastrar(
            interaction,
            tipo=TIPO_MANGA,
            externo_id=manga["id"],
            nome=titulo_mangadex(manga.get("attributes") or {}),
            url=f"https://mangadex.org/title/{manga['id']}",
            canal=destino,
            cargo=cargo,
            opcoes={"idiomas": escolhidos},
        )

    async def _achar_manga(self, titulo: str) -> dict[str, Any] | None:
        """Aceita tanto o uuid vindo do autocomplete quanto texto digitado."""
        if _UUID_RE.match(titulo.strip()):
            dados = await self.mangadex(f"/manga/{titulo.strip()}", [])
            return dados.get("data")
        resultados = await self.buscar_mangas(titulo)
        return resultados[0] if resultados else None

    @alerta.command(name="anime", description="Avisa quando sair episódio novo.")
    @app_commands.describe(
        titulo="Título do anime (escolha uma sugestão)",
        canal_discord="Onde avisar (padrão: este canal)",
        cargo="Cargo a mencionar no aviso",
    )
    @app_commands.checks.has_permissions(manage_guild=True)
    async def cmd_anime(
        self,
        interaction: discord.Interaction,
        titulo: str,
        canal_discord: discord.TextChannel | None = None,
        cargo: discord.Role | None = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        destino = await self._destino(interaction, canal_discord)
        if destino is None:
            return
        try:
            anime = await self._achar_anime(titulo)
        except FonteIndisponivel as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        if anime is None:
            await interaction.followup.send(
                f"Não achei nenhum anime com o título “{_cortar(titulo, 100)}”.",
                ephemeral=True,
            )
            return

        await self._cadastrar(
            interaction,
            tipo=TIPO_ANIME,
            externo_id=str(anime["id"]),
            nome=_titulo_anilist(anime),
            url=anime.get("siteUrl") or "",
            canal=destino,
            cargo=cargo,
        )

    async def _achar_anime(self, titulo: str) -> dict[str, Any] | None:
        alvo = titulo.strip()
        if alvo.isdigit():
            dados = await self.anilist(
                AGENDA_ANIME, {"id": int(alvo), "ate": int(time.time())}
            )
            return dados.get("Media")
        resultados = await self.buscar_animes(alvo)
        return resultados[0] if resultados else None

    @alerta.command(name="rss", description="Avisa quando sair item novo num feed.")
    @app_commands.describe(
        url="URL do feed RSS ou Atom",
        nome="Como esse feed aparece nos avisos",
        canal_discord="Onde avisar (padrão: este canal)",
        cargo="Cargo a mencionar no aviso",
    )
    @app_commands.checks.has_permissions(manage_guild=True)
    async def cmd_rss(
        self,
        interaction: discord.Interaction,
        url: str,
        nome: str,
        canal_discord: discord.TextChannel | None = None,
        cargo: discord.Role | None = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        destino = await self._destino(interaction, canal_discord)
        if destino is None:
            return

        endereco = url.strip()
        if not endereco.lower().startswith(("http://", "https://")):
            await interaction.followup.send(
                "A URL precisa começar com http:// ou https://.", ephemeral=True
            )
            return
        try:
            titulo_feed, itens = await self.ler_feed(endereco)
        except FonteIndisponivel as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        if not itens and not titulo_feed:
            await interaction.followup.send(
                "Não consegui ler nenhum item desse endereço. Ele é mesmo um feed"
                " RSS/Atom?",
                ephemeral=True,
            )
            return

        await self._cadastrar(
            interaction,
            tipo=TIPO_RSS,
            externo_id=endereco,
            nome=nome.strip() or titulo_feed or endereco,
            url=endereco,
            canal=destino,
            cargo=cargo,
        )

    @alerta.command(name="list", description="Assinaturas deste servidor.")
    async def cmd_list(self, interaction: discord.Interaction) -> None:
        assert interaction.guild_id is not None
        assinaturas = await listar_assinaturas(self.db, interaction.guild_id)
        if not assinaturas:
            await interaction.response.send_message(
                "Nenhuma assinatura ainda. Comece com `/alerta youtube`.", ephemeral=True
            )
            return

        linhas = []
        for assinatura in assinaturas:
            quando = (
                f"<t:{int(assinatura.ultima_checagem.timestamp())}:R>"
                if assinatura.ultima_checagem
                else "nunca"
            )
            alerta_falhas = (
                f" ⚠️ {assinatura.falhas} falha(s) seguidas" if assinatura.falhas else ""
            )
            nome = discord.utils.escape_markdown(_cortar(assinatura.nome, 80))
            linhas.append(
                f"`{assinatura.id:>3}` **{assinatura.rotulo}** · {nome}\n"
                f"→ <#{assinatura.channel_id}> · checado {quando}{alerta_falhas}"
            )

        embed = discord.Embed(
            title="Alertas assinados",
            description="\n".join(linhas[:20]),
            color=discord.Color.blurple(),
        )
        if len(linhas) > 20:
            embed.set_footer(text=f"Mostrando 20 de {len(linhas)}.")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @alerta.command(name="remover", description="Cancela uma assinatura.")
    @app_commands.describe(assinatura="Qual assinatura remover")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def cmd_remover(self, interaction: discord.Interaction, assinatura: str) -> None:
        assert interaction.guild_id is not None
        alvo = await self._assinatura_escolhida(interaction.guild_id, assinatura)
        if alvo is None:
            await interaction.response.send_message(
                "Não achei essa assinatura. Escolha uma da lista.", ephemeral=True
            )
            return
        await remover_assinatura(self.db, alvo.id)
        await interaction.response.send_message(
            f"Assinatura de **{_cortar(alvo.nome, 100)}** removida.", ephemeral=True
        )

    @alerta.command(
        name="testar", description="Checa uma assinatura agora, sem marcar nada."
    )
    @app_commands.describe(assinatura="Qual assinatura testar")
    # Cada uso é uma requisição a uma API externa, então vale a mesma exigência
    # dos comandos que escrevem.
    @app_commands.checks.has_permissions(manage_guild=True)
    async def cmd_testar(self, interaction: discord.Interaction, assinatura: str) -> None:
        assert interaction.guild_id is not None
        alvo = await self._assinatura_escolhida(interaction.guild_id, assinatura)
        if alvo is None:
            await interaction.response.send_message(
                "Não achei essa assinatura. Escolha uma da lista.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            itens = await self.coletar(alvo)
        except FonteIndisponivel as exc:
            await interaction.followup.send(
                f"A fonte não respondeu: {exc}", ephemeral=True
            )
            return
        if not itens:
            await interaction.followup.send(
                "A fonte respondeu, mas não tem nenhum item no momento.", ephemeral=True
            )
            return

        vistos = await ids_vistos(self.db, alvo.id)
        pendentes = [item for item in itens if item.id not in vistos]
        embed = self.embed_do_item(alvo, itens[0])
        embed.set_footer(
            text=f"Teste: nada foi marcado como visto. {len(pendentes)} item(ns)"
            " seriam avisados na próxima checagem."
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

    async def _assinatura_escolhida(
        self, guild_id: int, valor: str
    ) -> Assinatura | None:
        """O autocomplete manda o id; texto solto cai numa busca por nome."""
        if valor.strip().isdigit():
            alvo = await get_assinatura(self.db, int(valor))
            return alvo if alvo and alvo.guild_id == guild_id else None
        procurado = valor.strip().casefold()
        for assinatura in await listar_assinaturas(self.db, guild_id):
            if assinatura.nome.casefold() == procurado:
                return assinatura
        return None

    # -- autocomplete -------------------------------------------------------

    @cmd_manga.autocomplete("titulo")
    async def ac_manga(
        self, interaction: discord.Interaction, atual: str
    ) -> list[app_commands.Choice[str]]:
        if len(atual.strip()) < 2:
            return []
        try:
            resultados = await self.buscar_mangas(atual)
        except FonteIndisponivel:
            return []
        return [
            app_commands.Choice(
                name=_cortar(titulo_mangadex(item.get("attributes") or {}), 100),
                value=item["id"],
            )
            for item in resultados[:25]
        ]

    @cmd_anime.autocomplete("titulo")
    async def ac_anime(
        self, interaction: discord.Interaction, atual: str
    ) -> list[app_commands.Choice[str]]:
        if len(atual.strip()) < 2:
            return []
        try:
            resultados = await self.buscar_animes(atual)
        except FonteIndisponivel:
            return []
        escolhas = []
        for item in resultados[:25]:
            ano = (item.get("startDate") or {}).get("year")
            formato = item.get("format") or ""
            sufixo = " · ".join(str(parte) for parte in (formato, ano) if parte)
            rotulo = _titulo_anilist(item)
            if sufixo:
                rotulo = f"{rotulo} ({sufixo})"
            escolhas.append(
                app_commands.Choice(name=_cortar(rotulo, 100), value=str(item["id"]))
            )
        return escolhas

    @cmd_remover.autocomplete("assinatura")
    @cmd_testar.autocomplete("assinatura")
    async def ac_assinatura(
        self, interaction: discord.Interaction, atual: str
    ) -> list[app_commands.Choice[str]]:
        if interaction.guild_id is None:
            return []
        procurado = atual.casefold()
        escolhas = []
        for assinatura in await listar_assinaturas(self.db, interaction.guild_id):
            if procurado and procurado not in assinatura.nome.casefold():
                continue
            escolhas.append(
                app_commands.Choice(
                    name=_cortar(f"[{assinatura.rotulo}] {assinatura.nome}", 100),
                    value=str(assinatura.id),
                )
            )
        return escolhas[:25]


async def setup(bot: LeviathanBot) -> None:
    await bot.add_cog(Alertas(bot))
