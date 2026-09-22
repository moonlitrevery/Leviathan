"""Transforma mensagens em cards de citação, gerados com Pillow.

Reagir com o emoji configurado numa mensagem salva a citação no banco e publica o
card no canal de quotes. O texto e o nome do autor ficam gravados para que a
citação sobreviva à mensagem original ser apagada.

A fonte é embarcada em ``leviathan/assets/fonts`` e carregada por caminho absoluto:
o servidor de deploy não tem as fontes da máquina de desenvolvimento, e depender de
fonte do sistema só quebraria lá.

A Inter não tem glifos de emoji, então emoji não é desenhado como texto: o texto é
segmentado em trechos e cada emoji vira uma imagem colada no lugar. Os PNGs são
baixados **antes** do render (o render roda em ``asyncio.to_thread`` e não pode
esperar rede) e ficam em cache no disco.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path

import aiohttp
import discord
import emoji as emoji_lib
from discord import app_commands
from discord.ext import commands
from PIL import Image, ImageDraw, ImageFont, ImageOps

from leviathan.bot import LeviathanBot
from leviathan.config import PROJECT_ROOT
from leviathan.db import Database
from leviathan.emoji import emoji_key, parse_emoji

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Fonte embarcada
# ---------------------------------------------------------------------------

FONTS_DIR = Path(__file__).resolve().parent.parent / "assets" / "fonts"
FONT_PATH = FONTS_DIR / "Inter.ttf"


class QuotesAssetError(RuntimeError):
    """Recurso embarcado do cog ausente ou ilegível."""


try:
    # Lido uma vez para a memória: cada render cria fontes novas a partir destes
    # bytes, o que evita reler 850 KB do disco e evita compartilhar objetos de
    # fonte entre threads (o render roda em asyncio.to_thread).
    _FONT_BYTES = FONT_PATH.read_bytes()
except OSError as exc:
    log.error("Cog de quotes não carregado — fonte ausente em %s (%s)", FONT_PATH, exc)
    raise QuotesAssetError(f"fonte não encontrada em {FONT_PATH}") from exc


def _fonte(tamanho: int, peso: str = "Regular") -> ImageFont.FreeTypeFont:
    fonte = ImageFont.truetype(BytesIO(_FONT_BYTES), tamanho)
    fonte.set_variation_by_name(peso)
    return fonte


# ---------------------------------------------------------------------------
# Emoji: segmentação, sprites e cache
# ---------------------------------------------------------------------------

#: Fork mantido do Twemoji (o repositório original foi arquivado).
TWEMOJI_URL = "https://cdn.jsdelivr.net/gh/jdecked/twemoji@latest/assets/72x72/{nome}.png"
DISCORD_EMOJI_URL = "https://cdn.discordapp.com/emojis/{ident}.png"

#: PNGs baixados ficam aqui. data/ já é ignorado pelo git.
EMOJI_CACHE_DIR = PROJECT_ROOT / "data" / "emoji_cache"

CUSTOM_EMOJI_RE = re.compile(r"<(a?):(\w{1,32}):(\d{13,20})>")


@dataclass(frozen=True, slots=True)
class EmojiRef:
    """Um emoji encontrado no texto e onde buscar a imagem dele."""

    chave: str
    fallback: str
    urls: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Atomo:
    """Pedaço indivisível de uma palavra: um trecho de texto ou um emoji."""

    texto: str = ""
    emoji: EmojiRef | None = None


#: Uma palavra é uma sequência de átomos que não se separa na quebra de linha.
Palavra = tuple[Atomo, ...]

#: Sentinela de quebra de parágrafo, comparada por identidade.
QUEBRA: Palavra = (Atomo(texto="\n"),)


def _nomes_twemoji(sequencia: str) -> tuple[str, ...]:
    """Nomes de arquivo candidatos no Twemoji, do mais provável ao menos.

    O Twemoji nomeia pelos codepoints em hex separados por ``-`` e, na maioria dos
    casos, **sem** o U+FE0F: ``2764.png`` existe e ``2764-fe0f.png`` não. Mas há
    exceções, então tentamos as duas formas.
    """
    pontos = [f"{ord(c):x}" for c in sequencia]
    sem_vs = [p for p in pontos if p != "fe0f"]
    candidatos = []
    if sem_vs:
        candidatos.append("-".join(sem_vs))
    candidatos.append("-".join(pontos))
    return tuple(dict.fromkeys(candidatos))


def _ref_unicode(sequencia: str) -> EmojiRef:
    nome = emoji_lib.demojize(sequencia)
    if not nome.isascii():  # sem correspondência na tabela: evita devolver tofu
        nome = ":emoji:"
    return EmojiRef(
        chave="u-" + "-".join(f"{ord(c):x}" for c in sequencia),
        fallback=nome,
        urls=tuple(TWEMOJI_URL.format(nome=n) for n in _nomes_twemoji(sequencia)),
    )


def _spans(texto: str) -> list[tuple[int, int, EmojiRef]]:
    """Posições de todos os emoji do texto, custom e unicode."""
    spans: list[tuple[int, int, EmojiRef]] = []
    for achado in CUSTOM_EMOJI_RE.finditer(texto):
        _animado, nome, ident = achado.groups()
        spans.append(
            (
                achado.start(),
                achado.end(),
                EmojiRef(
                    chave=f"c-{ident}",
                    fallback=f":{nome}:",
                    urls=(DISCORD_EMOJI_URL.format(ident=ident),),
                ),
            )
        )

    ocupados = [(inicio, fim) for inicio, fim, _ in spans]
    # emoji_list cuida das sequências ZWJ e dos modificadores de tom de pele.
    for achado in emoji_lib.emoji_list(texto):
        inicio, fim = achado["match_start"], achado["match_end"]
        if any(inicio < f and i < fim for i, f in ocupados):
            continue
        spans.append((inicio, fim, _ref_unicode(achado["emoji"])))

    spans.sort(key=lambda span: span[0])
    return spans


def segmentar(texto: str) -> list[Palavra]:
    """Quebra o texto em palavras, cada uma feita de átomos de texto e de emoji."""
    palavras: list[Palavra] = []
    atual: list[Atomo] = []

    def fechar() -> None:
        nonlocal atual
        if atual:
            palavras.append(tuple(atual))
            atual = []

    def adicionar_texto(trecho: str) -> None:
        for parte in re.split(r"(\s+)", trecho):
            if not parte:
                continue
            if parte.isspace():
                fechar()
                for _ in range(parte.count("\n")):
                    palavras.append(QUEBRA)
            else:
                atual.append(Atomo(texto=parte))

    posicao = 0
    for inicio, fim, ref in _spans(texto):
        if inicio > posicao:
            adicionar_texto(texto[posicao:inicio])
        atual.append(Atomo(emoji=ref))
        posicao = fim
    if posicao < len(texto):
        adicionar_texto(texto[posicao:])
    fechar()
    return palavras


def refs_de(texto: str) -> dict[str, EmojiRef]:
    """Emoji distintos do texto, prontos para serem baixados antes do render."""
    return {ref.chave: ref for _, _, ref in _spans(texto)}


def _decodificar(imagens: dict[str, bytes]) -> dict[str, Image.Image]:
    """Converte os PNGs baixados em imagens.

    Feito antes de medir: se um PNG não decodificar, a chave fica de fora e tanto a
    medida quanto o desenho usam o texto ``:nome:``, sem descompasso entre os dois.
    """
    sprites: dict[str, Image.Image] = {}
    for chave, dados in imagens.items():
        try:
            imagem = Image.open(BytesIO(dados))
            imagem.load()
            sprites[chave] = imagem.convert("RGBA")
        except Exception:
            log.warning("Sprite de emoji %s não pôde ser decodificado", chave)
    return sprites


# ---------------------------------------------------------------------------
# Layout do card
# ---------------------------------------------------------------------------

LARGURA, ALTURA = 1200, 630
MARGEM = 72
AVATAR = 190
ESPACO_AVATAR = 56
GAP_CITACAO_AUTOR = 34
GAP_AUTOR_META = 12

FUNDO = (22, 24, 28)
COR_CITACAO = (242, 243, 245)
COR_NOME = (220, 221, 225)
COR_META = (138, 143, 152)
COR_AVATAR_FALLBACK = (88, 92, 100)

TAMANHO_MAX = 52
TAMANHO_MIN = 20
TAMANHO_NOME = 30
TAMANHO_META = 22
ALTURA_LINHA = 1.34

#: Acima disso a citação é cortada com reticências.
LIMITE_CARACTERES = 400

#: O card mostra a data no fuso de Brasília. O Brasil não usa horário de verão
#: desde 2019, então um offset fixo basta e evita depender do tzdata do servidor.
FUSO_EXIBICAO = timezone(timedelta(hours=-3))

MESES = (
    "janeiro", "fevereiro", "março", "abril", "maio", "junho",
    "julho", "agosto", "setembro", "outubro", "novembro", "dezembro",
)

EMOJI_PADRAO = "📌"


def formatar_data(quando: datetime) -> str:
    """``14 de setembro de 2026``, sem depender do locale do servidor."""
    if quando.tzinfo is None:
        quando = quando.replace(tzinfo=timezone.utc)
    local = quando.astimezone(FUSO_EXIBICAO)
    return f"{local.day} de {MESES[local.month - 1]} de {local.year}"


def truncar(texto: str, limite: int = LIMITE_CARACTERES) -> str:
    texto = texto.strip()
    if len(texto) <= limite:
        return texto
    return texto[:limite].rstrip() + "…"


def _largura_atomo(atomo: Atomo, fonte, tamanho: int, sprites) -> float:
    if atomo.emoji is None:
        return fonte.getlength(atomo.texto)
    if atomo.emoji.chave in sprites:
        # O emoji ocupa um quadrado do tamanho da fonte. Medir assim é o que impede
        # o texto de vazar a margem quando o card tem emoji.
        return float(tamanho)
    return fonte.getlength(atomo.emoji.fallback)


def _largura_palavra(palavra: Palavra, fonte, tamanho: int, sprites) -> float:
    return sum(_largura_atomo(atomo, fonte, tamanho, sprites) for atomo in palavra)


def _dividir_palavra(
    palavra: Palavra, fonte, tamanho: int, largura_max: float, sprites
) -> list[Palavra]:
    """Parte uma palavra que sozinha não cabe na linha (URL longa, por exemplo)."""
    pedacos: list[Palavra] = []
    atual: list[Atomo] = []
    largura = 0.0

    def empurrar(atomo: Atomo, quanto: float) -> None:
        nonlocal atual, largura
        if atual and largura + quanto > largura_max:
            pedacos.append(tuple(atual))
            atual = []
            largura = 0.0
        atual.append(atomo)
        largura += quanto

    for atomo in palavra:
        if atomo.emoji is not None:
            empurrar(atomo, _largura_atomo(atomo, fonte, tamanho, sprites))
            continue
        for caractere in atomo.texto:
            empurrar(Atomo(texto=caractere), fonte.getlength(caractere))

    if atual:
        pedacos.append(tuple(atual))
    return pedacos or [palavra]


def quebrar_linhas(
    palavras: list[Palavra], fonte, tamanho: int, largura_max: float, sprites
) -> list[list[Palavra]]:
    """Quebra automática por largura real, contando emoji como quadrado."""
    linhas: list[list[Palavra]] = []
    atual: list[Palavra] = []
    largura = 0.0
    espaco = fonte.getlength(" ")

    for palavra in palavras:
        if palavra is QUEBRA:
            linhas.append(atual)
            atual = []
            largura = 0.0
            continue

        propria = _largura_palavra(palavra, fonte, tamanho, sprites)
        if propria > largura_max:
            if atual:
                linhas.append(atual)
                atual = []
                largura = 0.0
            pedacos = _dividir_palavra(palavra, fonte, tamanho, largura_max, sprites)
            for pedaco in pedacos[:-1]:
                linhas.append([pedaco])
            atual = [pedacos[-1]]
            largura = _largura_palavra(pedacos[-1], fonte, tamanho, sprites)
            continue

        extra = propria if not atual else espaco + propria
        if atual and largura + extra > largura_max:
            linhas.append(atual)
            atual = [palavra]
            largura = propria
        else:
            atual.append(palavra)
            largura += extra

    if atual:
        linhas.append(atual)
    return linhas


def _ajustar_citacao(
    palavras: list[Palavra], largura_max: float, altura_max: float, sprites
):
    """Maior tamanho de fonte em que o texto ainda cabe no espaço disponível."""
    for tamanho in range(TAMANHO_MAX, TAMANHO_MIN - 1, -2):
        fonte = _fonte(tamanho, "Medium")
        linhas = quebrar_linhas(palavras, fonte, tamanho, largura_max, sprites)
        altura_linha = round(tamanho * ALTURA_LINHA)
        if len(linhas) * altura_linha <= altura_max:
            return fonte, tamanho, linhas, altura_linha

    # Salvaguarda: no menor tamanho, corta o que não couber.
    fonte = _fonte(TAMANHO_MIN, "Medium")
    altura_linha = round(TAMANHO_MIN * ALTURA_LINHA)
    linhas = quebrar_linhas(palavras, fonte, TAMANHO_MIN, largura_max, sprites)
    cabem = max(int(altura_max // altura_linha), 1)
    if len(linhas) > cabem:
        linhas = linhas[:cabem]
        linhas[-1] = [*linhas[-1], (Atomo(texto="…"),)]
    return fonte, TAMANHO_MIN, linhas, altura_linha


def _desenhar_linha(
    imagem: Image.Image,
    desenho: ImageDraw.ImageDraw,
    linha: list[Palavra],
    x: float,
    y: int,
    fonte,
    tamanho: int,
    sprites,
    ascent: int,
) -> None:
    espaco = fonte.getlength(" ")
    cursor = float(x)
    for indice, palavra in enumerate(linha):
        if indice:
            cursor += espaco
        for atomo in palavra:
            if atomo.emoji is None:
                desenho.text((cursor, y), atomo.texto, font=fonte, fill=COR_CITACAO)
                cursor += fonte.getlength(atomo.texto)
                continue

            sprite = sprites.get(atomo.emoji.chave)
            if sprite is None:
                desenho.text(
                    (cursor, y), atomo.emoji.fallback, font=fonte, fill=COR_CITACAO
                )
                cursor += fonte.getlength(atomo.emoji.fallback)
                continue

            redimensionado = sprite.resize((tamanho, tamanho), Image.LANCZOS)
            topo = y + round((ascent - tamanho) / 2)
            imagem.paste(redimensionado, (round(cursor), topo), redimensionado)
            cursor += tamanho


def _avatar_circular(dados: bytes | None, tamanho: int) -> Image.Image:
    """Recorta o avatar em círculo; cai para um círculo cinza se não der."""
    escala = 4
    mascara = Image.new("L", (tamanho * escala, tamanho * escala), 0)
    ImageDraw.Draw(mascara).ellipse(
        (0, 0, tamanho * escala - 1, tamanho * escala - 1), fill=255
    )
    mascara = mascara.resize((tamanho, tamanho), Image.LANCZOS)

    base: Image.Image | None = None
    if dados:
        try:
            aberta = Image.open(BytesIO(dados))
            aberta.load()
            base = ImageOps.fit(aberta.convert("RGB"), (tamanho, tamanho), Image.LANCZOS)
        except Exception:  # Pillow levanta vários tipos para arquivo inválido
            log.warning("Avatar baixado não pôde ser decodificado, usando fallback")
            base = None
    if base is None:
        base = Image.new("RGB", (tamanho, tamanho), COR_AVATAR_FALLBACK)

    saida = Image.new("RGBA", (tamanho, tamanho), (0, 0, 0, 0))
    saida.paste(base, (0, 0))
    saida.putalpha(mascara)
    return saida


def render_quote_card(
    *,
    texto: str,
    autor: str,
    data: datetime,
    canal: str,
    avatar_bytes: bytes | None = None,
    emoji_imagens: dict[str, bytes] | None = None,
) -> bytes:
    """Gera o PNG do card. Bloqueante: chame via :func:`asyncio.to_thread`."""
    texto = truncar(texto)
    sprites = _decodificar(emoji_imagens or {})

    imagem = Image.new("RGB", (LARGURA, ALTURA), FUNDO)
    desenho = ImageDraw.Draw(imagem)

    avatar = _avatar_circular(avatar_bytes, AVATAR)
    imagem.paste(avatar, (MARGEM, (ALTURA - AVATAR) // 2), avatar)

    x = MARGEM + AVATAR + ESPACO_AVATAR
    largura_max = LARGURA - MARGEM - x

    fonte_nome = _fonte(TAMANHO_NOME, "SemiBold")
    fonte_meta = _fonte(TAMANHO_META, "Regular")
    altura_nome = round(TAMANHO_NOME * 1.3)
    altura_meta = round(TAMANHO_META * 1.3)
    bloco_autoria = altura_nome + GAP_AUTOR_META + altura_meta

    disponivel = ALTURA - 2 * MARGEM
    altura_max_citacao = disponivel - bloco_autoria - GAP_CITACAO_AUTOR

    palavras = segmentar(f"“{texto}”")
    fonte, tamanho, linhas, altura_linha = _ajustar_citacao(
        palavras, largura_max, altura_max_citacao, sprites
    )
    ascent = fonte.getmetrics()[0]

    altura_total = len(linhas) * altura_linha + GAP_CITACAO_AUTOR + bloco_autoria
    y = (ALTURA - altura_total) // 2

    for linha in linhas:
        _desenhar_linha(imagem, desenho, linha, x, y, fonte, tamanho, sprites, ascent)
        y += altura_linha

    y += GAP_CITACAO_AUTOR
    desenho.text((x, y), f"— {autor}", font=fonte_nome, fill=COR_NOME)
    y += altura_nome + GAP_AUTOR_META
    desenho.text(
        (x, y), f"{formatar_data(data)} · #{canal}", font=fonte_meta, fill=COR_META
    )

    buffer = BytesIO()
    imagem.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()


# ---------------------------------------------------------------------------
# Banco
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class QuoteConfig:
    guild_id: int
    emoji_key: str
    emoji_display: str
    channel_id: int


@dataclass(frozen=True, slots=True)
class Quote:
    id: int
    guild_id: int
    message_id: int
    channel_id: int
    author_id: int
    author_name: str
    content: str
    created_at: datetime
    jump_url: str
    saved_by: int
    card_posted: bool


async def get_quote_config(db: Database, guild_id: int) -> QuoteConfig | None:
    row = await db.fetchone(
        "SELECT guild_id, emoji_key, emoji_display, channel_id"
        " FROM quote_config WHERE guild_id = ?",
        (guild_id,),
    )
    if row is None:
        return None
    return QuoteConfig(
        guild_id=row["guild_id"],
        emoji_key=row["emoji_key"],
        emoji_display=row["emoji_display"],
        channel_id=row["channel_id"],
    )


async def set_quote_config(
    db: Database, guild_id: int, chave: str, display: str, channel_id: int
) -> None:
    await db.execute(
        "INSERT INTO quote_config (guild_id, emoji_key, emoji_display, channel_id)"
        " VALUES (?, ?, ?, ?)"
        " ON CONFLICT(guild_id) DO UPDATE SET"
        "   emoji_key = excluded.emoji_key,"
        "   emoji_display = excluded.emoji_display,"
        "   channel_id = excluded.channel_id,"
        "   updated_at = datetime('now')",
        (guild_id, chave, display, channel_id),
    )


def _quote_from_row(row) -> Quote:
    return Quote(
        id=row["id"],
        guild_id=row["guild_id"],
        message_id=row["message_id"],
        channel_id=row["channel_id"],
        author_id=row["author_id"],
        author_name=row["author_name"],
        content=row["content"],
        created_at=datetime.fromisoformat(row["created_at"]),
        jump_url=row["jump_url"],
        saved_by=row["saved_by"],
        card_posted=bool(row["card_posted"]),
    )


_QUOTE_COLUMNS = (
    "id, guild_id, message_id, channel_id, author_id, author_name,"
    " content, created_at, jump_url, saved_by, card_posted"
)


async def save_quote(
    db: Database,
    *,
    guild_id: int,
    message_id: int,
    channel_id: int,
    author_id: int,
    author_name: str,
    content: str,
    created_at: datetime,
    jump_url: str,
    saved_by: int,
) -> bool:
    """Salva a citação com ``card_posted = 0``. ``False`` se a mensagem já existia.

    O ``UNIQUE`` em ``message_id`` é o que garante "uma vez por mensagem": a
    segunda reação esbarra nele e o ``INSERT OR IGNORE`` vira no-op.
    """
    afetadas = await db.execute(
        "INSERT OR IGNORE INTO quotes"
        " (guild_id, message_id, channel_id, author_id, author_name,"
        "  content, created_at, jump_url, saved_by, card_posted)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0)",
        (
            guild_id,
            message_id,
            channel_id,
            author_id,
            author_name,
            content,
            created_at.isoformat(),
            jump_url,
            saved_by,
        ),
    )
    return afetadas > 0


async def get_quote_by_message(db: Database, message_id: int) -> Quote | None:
    row = await db.fetchone(
        f"SELECT {_QUOTE_COLUMNS} FROM quotes WHERE message_id = ?", (message_id,)
    )
    return _quote_from_row(row) if row else None


async def mark_card_posted(db: Database, quote_id: int) -> None:
    await db.execute("UPDATE quotes SET card_posted = 1 WHERE id = ?", (quote_id,))


async def random_quote(
    db: Database, guild_id: int, author_id: int | None = None
) -> Quote | None:
    if author_id is None:
        row = await db.fetchone(
            f"SELECT {_QUOTE_COLUMNS} FROM quotes WHERE guild_id = ?"
            " ORDER BY RANDOM() LIMIT 1",
            (guild_id,),
        )
    else:
        row = await db.fetchone(
            f"SELECT {_QUOTE_COLUMNS} FROM quotes WHERE guild_id = ? AND author_id = ?"
            " ORDER BY RANDOM() LIMIT 1",
            (guild_id, author_id),
        )
    return _quote_from_row(row) if row else None


async def count_quotes(db: Database, guild_id: int) -> int:
    row = await db.fetchone(
        "SELECT COUNT(*) AS total FROM quotes WHERE guild_id = ?", (guild_id,)
    )
    return int(row["total"]) if row else 0


async def top_quoted(db: Database, guild_id: int, limit: int) -> list[tuple[int, str, int]]:
    """``[(author_id, nome, quantidade)]`` de quem mais foi citado."""
    rows = await db.fetchall(
        "SELECT author_id, MAX(author_name) AS nome, COUNT(*) AS total FROM quotes"
        " WHERE guild_id = ?"
        " GROUP BY author_id"
        " ORDER BY total DESC, author_id ASC"
        " LIMIT ?",
        (guild_id, limit),
    )
    return [(int(r["author_id"]), r["nome"], int(r["total"])) for r in rows]


# ---------------------------------------------------------------------------
# Cog
# ---------------------------------------------------------------------------


class Quotes(commands.Cog):
    """Cards de citação a partir de reações."""

    quote = app_commands.Group(
        name="quote",
        description="Citações salvas em card de imagem.",
        guild_only=True,
    )

    def __init__(self, bot: LeviathanBot) -> None:
        self.bot = bot
        self.db = bot.db
        self._sessao: aiohttp.ClientSession | None = None
        # Emoji que já falharam: evita repetir o download a cada card.
        self._emoji_sem_sprite: set[str] = set()

    async def cog_load(self) -> None:
        self._sessao = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=10),
            headers={"User-Agent": "LeviathanBot/0.1 (quote cards)"},
        )

    async def cog_unload(self) -> None:
        if self._sessao is not None:
            await self._sessao.close()
            self._sessao = None

    # -- download ----------------------------------------------------------

    async def _baixar(self, url: str) -> bytes | None:
        if self._sessao is None:
            return None
        try:
            async with self._sessao.get(url) as resposta:
                if resposta.status != 200:
                    return None
                return await resposta.read()
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            log.warning("Falha ao baixar %s: %s", url, exc)
            return None

    async def baixar_avatar(self, url: str) -> bytes | None:
        """Baixa o avatar. Devolve ``None`` em qualquer falha: o card tem fallback."""
        dados = await self._baixar(url)
        if dados is None:
            log.warning("Avatar %s indisponível, usando o círculo cinza", url)
        return dados

    async def obter_sprite(self, ref: EmojiRef) -> bytes | None:
        """PNG do emoji, do cache em disco ou da rede."""
        caminho = EMOJI_CACHE_DIR / f"{ref.chave}.png"
        try:
            return caminho.read_bytes()
        except OSError:
            pass

        if ref.chave in self._emoji_sem_sprite:
            return None

        for url in ref.urls:
            dados = await self._baixar(url)
            if dados:
                try:
                    EMOJI_CACHE_DIR.mkdir(parents=True, exist_ok=True)
                    caminho.write_bytes(dados)
                except OSError as exc:
                    log.warning("Não consegui gravar o cache de %s: %s", ref.chave, exc)
                return dados

        self._emoji_sem_sprite.add(ref.chave)
        log.info(
            "Sem sprite para o emoji %s, o card vai mostrar %s", ref.chave, ref.fallback
        )
        return None

    async def preparar_emojis(self, texto: str) -> dict[str, bytes]:
        """Baixa os emoji do texto antes do render, que roda numa thread sem rede."""
        imagens: dict[str, bytes] = {}
        for chave, ref in refs_de(texto).items():
            dados = await self.obter_sprite(ref)
            if dados is not None:
                imagens[chave] = dados
        return imagens

    async def _avatar_de(self, guild: discord.Guild, author_id: int) -> bytes | None:
        membro = guild.get_member(author_id)
        if membro is None:
            return None
        return await self.baixar_avatar(membro.display_avatar.replace(size=256).url)

    async def gerar_card(
        self,
        *,
        texto: str,
        autor: str,
        data: datetime,
        canal: str,
        avatar_bytes: bytes | None,
    ) -> discord.File:
        emoji_imagens = await self.preparar_emojis(texto)
        # Pillow é bloqueante: fora do event loop, senão trava o bot inteiro.
        png = await asyncio.to_thread(
            render_quote_card,
            texto=texto,
            autor=autor,
            data=data,
            canal=canal,
            avatar_bytes=avatar_bytes,
            emoji_imagens=emoji_imagens,
        )
        return discord.File(BytesIO(png), filename="quote.png")

    # -- evento ------------------------------------------------------------

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent) -> None:
        if payload.guild_id is None:
            return
        if payload.member is not None and payload.member.bot:
            return

        config = await get_quote_config(self.db, payload.guild_id)
        if config is None or emoji_key(payload.emoji) != config.emoji_key:
            return
        # Reagir dentro do próprio canal de quotes não gera card de card.
        if payload.channel_id == config.channel_id:
            return

        guild = self.bot.get_guild(payload.guild_id)
        if guild is None:
            return

        existente = await get_quote_by_message(self.db, payload.message_id)
        if existente is not None and existente.card_posted:
            log.debug("Mensagem %s já virou card", payload.message_id)
            return

        mensagem = await self._buscar_mensagem(payload.channel_id, payload.message_id)
        if mensagem is None or mensagem.author.bot:
            return

        # clean_content resolve <@id> para @Nome e <#id> para #canal. É o que vai
        # para o banco também: o jogo de adivinhação lê dali e não pode vazar id.
        conteudo = mensagem.clean_content
        if not conteudo.strip():
            log.debug("Mensagem %s ignorada: sem texto", payload.message_id)
            return

        if existente is None:
            await save_quote(
                self.db,
                guild_id=payload.guild_id,
                message_id=mensagem.id,
                channel_id=mensagem.channel.id,
                author_id=mensagem.author.id,
                author_name=mensagem.author.display_name,
                content=conteudo,
                created_at=mensagem.created_at,
                jump_url=mensagem.jump_url,
                saved_by=payload.user_id,
            )
            existente = await get_quote_by_message(self.db, mensagem.id)
            if existente is None:  # não deveria acontecer
                log.error("Citação da mensagem %s sumiu logo após salvar", mensagem.id)
                return
        else:
            log.info(
                "Retentando o card da mensagem %s, que ficou com card_posted = 0",
                mensagem.id,
            )

        destino = guild.get_channel(config.channel_id)
        if not isinstance(destino, discord.abc.Messageable):
            log.warning(
                "Canal de quotes %d inacessível; a citação %d fica pendente",
                config.channel_id,
                existente.id,
            )
            return

        avatar = await self.baixar_avatar(
            mensagem.author.display_avatar.replace(size=256).url
        )
        try:
            arquivo = await self.gerar_card(
                texto=existente.content,
                autor=existente.author_name,
                data=existente.created_at,
                canal=getattr(mensagem.channel, "name", "desconhecido"),
                avatar_bytes=avatar,
            )
            await destino.send(file=arquivo)
        except (discord.HTTPException, OSError, ValueError) as exc:
            log.exception(
                "Falha ao publicar o card da citação %d (mensagem %s): %s."
                " Fica pendente e a próxima reação tenta de novo",
                existente.id,
                mensagem.id,
                exc,
            )
            return

        await mark_card_posted(self.db, existente.id)

    async def _buscar_mensagem(
        self, channel_id: int, message_id: int
    ) -> discord.Message | None:
        canal = self.bot.get_channel(channel_id)
        if canal is None:
            try:
                canal = await self.bot.fetch_channel(channel_id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                return None
        if not isinstance(canal, discord.abc.Messageable):
            return None
        try:
            return await canal.fetch_message(message_id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return None

    # -- comandos ----------------------------------------------------------

    async def _contador_com_mesmo_emoji(self, guild_id: int, chave: str):
        """Contador do cog piadas que usa este emoji, se o cog estiver disponível."""
        try:
            from leviathan.cogs.piadas import get_counter_by_emoji
        except ImportError:
            log.debug("Cog piadas indisponível: pulando a checagem de colisão de emoji")
            return None
        try:
            return await get_counter_by_emoji(self.db, guild_id, chave)
        except Exception:
            log.warning("Não consegui checar colisão de emoji com os contadores")
            return None

    @quote.command(name="config", description="Define o emoji e o canal das citações.")
    @app_commands.describe(
        emoji=f"Emoji que salva a citação (sugestão: {EMOJI_PADRAO})",
        canal="Canal onde os cards são publicados",
    )
    @app_commands.checks.has_permissions(manage_guild=True)
    async def quote_config(
        self,
        interaction: discord.Interaction,
        emoji: str,
        canal: discord.TextChannel,
    ) -> None:
        assert interaction.guild_id is not None

        parsed = parse_emoji(emoji)
        if parsed is None:
            await interaction.response.send_message(
                f"`{emoji}` não parece um emoji. Use um emoji unicode "
                f"({EMOJI_PADRAO}) ou um emoji custom deste servidor.",
                ephemeral=True,
            )
            return

        permissoes = canal.permissions_for(canal.guild.me)
        if not (permissoes.send_messages and permissoes.attach_files):
            await interaction.response.send_message(
                f"Não consigo publicar em {canal.mention}: preciso de "
                "'Enviar mensagens' e 'Anexar arquivos'.",
                ephemeral=True,
            )
            return

        chave = emoji_key(parsed)
        await set_quote_config(self.db, interaction.guild_id, chave, str(parsed), canal.id)

        linhas = [
            f"Citações configuradas: reaja com {parsed} e o card vai para {canal.mention}."
        ]
        colisao = await self._contador_com_mesmo_emoji(interaction.guild_id, chave)
        if colisao is not None:
            linhas.append(
                f"⚠️ Atenção: {parsed} já alimenta o contador **{colisao.name}**. "
                "Reagir com ele vai incrementar o contador **e** criar um card. "
                "Se não for isso que você quer, escolha outro emoji aqui ou em "
                "`/contador remover`."
            )

        await interaction.response.send_message("\n\n".join(linhas), ephemeral=True)

    @quote.command(name="random", description="Mostra uma citação salva ao acaso.")
    async def quote_random(self, interaction: discord.Interaction) -> None:
        await self._responder_com_citacao(interaction, autor=None)

    @quote.command(name="de", description="Mostra uma citação ao acaso de alguém.")
    @app_commands.describe(membro="De quem você quer uma citação")
    async def quote_de(
        self, interaction: discord.Interaction, membro: discord.Member
    ) -> None:
        await self._responder_com_citacao(interaction, autor=membro)

    async def _responder_com_citacao(
        self, interaction: discord.Interaction, *, autor: discord.Member | None
    ) -> None:
        assert interaction.guild_id is not None and interaction.guild is not None

        citacao = await random_quote(
            self.db, interaction.guild_id, autor.id if autor else None
        )
        if citacao is None:
            alvo = f" de {autor.display_name}" if autor else ""
            await interaction.response.send_message(
                f"Não tenho nenhuma citação salva{alvo} ainda.", ephemeral=True
            )
            return

        await interaction.response.defer(thinking=True)

        avatar = await self._avatar_de(interaction.guild, citacao.author_id)
        canal = interaction.guild.get_channel(citacao.channel_id)

        arquivo = await self.gerar_card(
            texto=citacao.content,
            autor=citacao.author_name,
            data=citacao.created_at,
            canal=getattr(canal, "name", "desconhecido"),
            avatar_bytes=avatar,
        )
        await interaction.followup.send(file=arquivo)

    @quote.command(name="count", description="Quantas citações estão salvas.")
    async def quote_count(self, interaction: discord.Interaction) -> None:
        assert interaction.guild_id is not None

        total = await count_quotes(self.db, interaction.guild_id)
        if not total:
            await interaction.response.send_message(
                "Nenhuma citação salva ainda.", ephemeral=True
            )
            return

        top = await top_quoted(self.db, interaction.guild_id, 5)
        linhas = [
            f"`{posicao}.` <@{author_id}> — **{quantidade}**"
            for posicao, (author_id, _nome, quantidade) in enumerate(top, start=1)
        ]

        embed = discord.Embed(
            title=f"{total} citação salva" if total == 1 else f"{total} citações salvas",
            description="\n".join(linhas),
            color=discord.Color.blurple(),
        )
        embed.set_author(name="Mais citados")
        await interaction.response.send_message(embed=embed)


async def setup(bot: LeviathanBot) -> None:
    await bot.add_cog(Quotes(bot))
