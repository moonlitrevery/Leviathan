"""Transforma mensagens em cards de citação, gerados com Pillow.

Reagir com o emoji configurado numa mensagem salva a citação no banco e publica o
card no canal de quotes. O texto e o nome do autor ficam gravados para que a
citação sobreviva à mensagem original ser apagada.

A fonte é embarcada em ``leviathan/assets/fonts`` e carregada por caminho absoluto:
o servidor de deploy não tem as fontes da máquina de desenvolvimento, e depender de
fonte do sistema só quebraria lá.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands
from PIL import Image, ImageDraw, ImageFont, ImageOps

from leviathan.bot import LeviathanBot
from leviathan.cogs.piadas import emoji_key, get_counter_by_emoji, parse_emoji
from leviathan.db import Database

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


def _quebrar_palavra(palavra: str, fonte, largura_max: float) -> list[str]:
    """Parte uma palavra que sozinha não cabe na linha (URL longa, por exemplo)."""
    partes: list[str] = []
    atual = ""
    for caractere in palavra:
        if not atual or fonte.getlength(atual + caractere) <= largura_max:
            atual += caractere
        else:
            partes.append(atual)
            atual = caractere
    if atual:
        partes.append(atual)
    return partes


def quebrar_linhas(texto: str, fonte, largura_max: float) -> list[str]:
    """Quebra automática por largura real do texto renderizado."""
    linhas: list[str] = []
    for paragrafo in texto.split("\n"):
        if not paragrafo.strip():
            linhas.append("")
            continue
        atual = ""
        for palavra in paragrafo.split():
            if fonte.getlength(palavra) > largura_max:
                if atual:
                    linhas.append(atual)
                    atual = ""
                pedacos = _quebrar_palavra(palavra, fonte, largura_max)
                linhas.extend(pedacos[:-1])
                atual = pedacos[-1]
                continue
            candidata = f"{atual} {palavra}".strip()
            if not atual or fonte.getlength(candidata) <= largura_max:
                atual = candidata
            else:
                linhas.append(atual)
                atual = palavra
        if atual:
            linhas.append(atual)
    return linhas


def _ajustar_citacao(texto: str, largura_max: float, altura_max: float):
    """Maior tamanho de fonte em que o texto ainda cabe no espaço disponível."""
    for tamanho in range(TAMANHO_MAX, TAMANHO_MIN - 1, -2):
        fonte = _fonte(tamanho, "Medium")
        linhas = quebrar_linhas(texto, fonte, largura_max)
        altura_linha = round(tamanho * ALTURA_LINHA)
        if len(linhas) * altura_linha <= altura_max:
            return fonte, linhas, altura_linha

    # Salvaguarda: no menor tamanho, corta o que não couber.
    fonte = _fonte(TAMANHO_MIN, "Medium")
    altura_linha = round(TAMANHO_MIN * ALTURA_LINHA)
    linhas = quebrar_linhas(texto, fonte, largura_max)
    cabem = max(int(altura_max // altura_linha), 1)
    if len(linhas) > cabem:
        linhas = linhas[:cabem]
        linhas[-1] = linhas[-1].rstrip() + "…"
    return fonte, linhas, altura_linha


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
) -> bytes:
    """Gera o PNG do card. Bloqueante: chame via :func:`asyncio.to_thread`."""
    texto = truncar(texto)

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

    fonte, linhas, altura_linha = _ajustar_citacao(
        f"“{texto}”", largura_max, altura_max_citacao
    )

    altura_total = len(linhas) * altura_linha + GAP_CITACAO_AUTOR + bloco_autoria
    y = (ALTURA - altura_total) // 2

    for linha in linhas:
        desenho.text((x, y), linha, font=fonte, fill=COR_CITACAO)
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
    )


_QUOTE_COLUMNS = (
    "id, guild_id, message_id, channel_id, author_id, author_name,"
    " content, created_at, jump_url, saved_by"
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
    """Salva a citação. Devolve ``False`` se a mensagem já tinha virado card.

    O ``UNIQUE`` em ``message_id`` é o que garante "uma vez por mensagem": a
    segunda reação esbarra nele e o ``INSERT OR IGNORE`` vira no-op.
    """
    afetadas = await db.execute(
        "INSERT OR IGNORE INTO quotes"
        " (guild_id, message_id, channel_id, author_id, author_name,"
        "  content, created_at, jump_url, saved_by)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
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

    async def cog_load(self) -> None:
        self._sessao = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=10),
            headers={"User-Agent": "LeviathanBot/0.1 (quote cards)"},
        )

    async def cog_unload(self) -> None:
        if self._sessao is not None:
            await self._sessao.close()
            self._sessao = None

    # -- avatar ------------------------------------------------------------

    async def baixar_avatar(self, url: str) -> bytes | None:
        """Baixa o avatar. Devolve ``None`` em qualquer falha: o card tem fallback."""
        if self._sessao is None:
            return None
        try:
            async with self._sessao.get(url) as resposta:
                if resposta.status != 200:
                    log.warning("Avatar %s devolveu HTTP %d", url, resposta.status)
                    return None
                return await resposta.read()
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            log.warning("Falha ao baixar o avatar %s: %s", url, exc)
            return None

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
        # Pillow é bloqueante: fora do event loop, senão trava o bot inteiro.
        png = await asyncio.to_thread(
            render_quote_card,
            texto=texto,
            autor=autor,
            data=data,
            canal=canal,
            avatar_bytes=avatar_bytes,
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

        mensagem = await self._buscar_mensagem(payload.channel_id, payload.message_id)
        if mensagem is None or mensagem.author.bot:
            return
        if not mensagem.content.strip():
            log.debug("Mensagem %s ignorada: sem texto", payload.message_id)
            return

        autor = mensagem.author
        nome = autor.display_name
        novo = await save_quote(
            self.db,
            guild_id=payload.guild_id,
            message_id=mensagem.id,
            channel_id=mensagem.channel.id,
            author_id=autor.id,
            author_name=nome,
            content=mensagem.content,
            created_at=mensagem.created_at,
            jump_url=mensagem.jump_url,
            saved_by=payload.user_id,
        )
        if not novo:
            log.debug("Mensagem %s já tinha virado card", mensagem.id)
            return

        destino = guild.get_channel(config.channel_id)
        if not isinstance(destino, discord.abc.Messageable):
            log.warning("Canal de quotes %d inacessível", config.channel_id)
            return

        avatar = await self.baixar_avatar(autor.display_avatar.replace(size=256).url)
        nome_canal = getattr(mensagem.channel, "name", "desconhecido")
        try:
            arquivo = await self.gerar_card(
                texto=mensagem.content,
                autor=nome,
                data=mensagem.created_at,
                canal=nome_canal,
                avatar_bytes=avatar,
            )
            await destino.send(file=arquivo)
        except (discord.HTTPException, OSError):
            log.exception("Falha ao publicar o card da mensagem %s", mensagem.id)

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
        # O mesmo emoji alimentando um contador faria as duas coisas dispararem juntas.
        colisao = await get_counter_by_emoji(self.db, interaction.guild_id, chave)
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
        nome_canal = getattr(canal, "name", "desconhecido")

        arquivo = await self.gerar_card(
            texto=citacao.content,
            autor=citacao.author_name,
            data=citacao.created_at,
            canal=nome_canal,
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
