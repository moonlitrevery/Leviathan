"""Integração com a Last.fm: o que está tocando, top e compatibilidade musical.

Precisa de ``LASTFM_API_KEY`` no ``.env``. Sem a chave, o ``setup`` levanta
:class:`~leviathan.bot.CogUnavailable` e o bot registra um aviso de uma linha —
os comandos ``/fm`` ficam de fora e o resto sobe normalmente.

As chamadas usam a sessão HTTP compartilhada do bot e passam por um cache curto,
em memória, para que vários comandos seguidos não martelem a API.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands

from leviathan.bot import CogUnavailable, LeviathanBot
from leviathan.db import Database

log = logging.getLogger(__name__)

LASTFM_URL = "https://ws.audioscrobbler.com/2.0/"
COR = discord.Color.from_str("#d51007")

#: A Last.fm devolve esta imagem de estrela quando a faixa não tem capa.
PLACEHOLDER_HASH = "2a96cbd8b46e442fc41c2b86b821562f"

#: Janela do cache em memória, por (método, parâmetros).
CACHE_TTL = 30.0

#: Teto por chamada. A sessão do bot já usa 10s, mas deixamos explícito aqui
#: porque este cog faz várias chamadas encadeadas por comando.
TIMEOUT = aiohttp.ClientTimeout(total=10)

TOP_LIMITE = 10
RECENTES_LIMITE = 10
COMPAT_TOP = 50
COMPAT_DESTAQUE = 5

#: valor da API -> (rótulo, método, chave da raiz, chave do item)
TIPOS: dict[str, tuple[str, str, str, str]] = {
    "artists": ("artistas", "user.getTopArtists", "topartists", "artist"),
    "tracks": ("músicas", "user.getTopTracks", "toptracks", "track"),
    "albums": ("álbuns", "user.getTopAlbums", "topalbums", "album"),
}

PERIODOS: dict[str, str] = {
    "7day": "últimos 7 dias",
    "1month": "último mês",
    "3month": "últimos 3 meses",
    "6month": "últimos 6 meses",
    "12month": "último ano",
    "overall": "desde sempre",
}

TIPO_CHOICES = [
    app_commands.Choice(name="artistas", value="artists"),
    app_commands.Choice(name="músicas", value="tracks"),
    app_commands.Choice(name="álbuns", value="albums"),
]

PERIODO_CHOICES = [
    app_commands.Choice(name="7 dias", value="7day"),
    app_commands.Choice(name="1 mês", value="1month"),
    app_commands.Choice(name="3 meses", value="3month"),
    app_commands.Choice(name="6 meses", value="6month"),
    app_commands.Choice(name="1 ano", value="12month"),
    app_commands.Choice(name="sempre", value="overall"),
]


# ---------------------------------------------------------------------------
# Erros
# ---------------------------------------------------------------------------


class LastfmError(Exception):
    """Erro já traduzido para uma mensagem que pode ir direto ao usuário."""


class LastfmUsuarioNaoEncontrado(LastfmError):
    pass


class LastfmPerfilPrivado(LastfmError):
    pass


class LastfmIndisponivel(LastfmError):
    pass


def _traduzir_erro(codigo: int, mensagem: str) -> LastfmError:
    """Converte o código de erro da Last.fm em algo legível em português."""
    if codigo == 6:
        return LastfmUsuarioNaoEncontrado(
            "Esse usuário não existe na Last.fm. Confira se o nome está certo."
        )
    if codigo == 17:
        return LastfmPerfilPrivado(
            "Esse perfil está com o histórico de faixas oculto. Em "
            "last.fm/settings/privacy, ligue 'Recent listening' para o bot conseguir ler."
        )
    if codigo == 29:
        return LastfmIndisponivel(
            "A Last.fm está limitando as requisições agora. Tente de novo em instantes."
        )
    if codigo in (10, 26):
        return LastfmIndisponivel(
            "A chave da API da Last.fm foi recusada. Confira o LASTFM_API_KEY no .env."
        )
    if codigo in (8, 11, 16):
        return LastfmIndisponivel(
            "A Last.fm está instável no momento. Tente de novo em instantes."
        )
    log.warning("Erro não mapeado da Last.fm: %s — %s", codigo, mensagem)
    return LastfmIndisponivel(f"A Last.fm recusou a consulta ({codigo}).")


# ---------------------------------------------------------------------------
# Cliente
# ---------------------------------------------------------------------------


def _como_lista(valor: Any) -> list[dict[str, Any]]:
    """A Last.fm devolve um objeto solto quando o resultado tem um item só."""
    if valor is None:
        return []
    if isinstance(valor, list):
        return valor
    return [valor]


def escolher_capa(imagens: Any) -> str | None:
    """Maior capa disponível, ou ``None`` se só houver o placeholder de estrela."""
    por_tamanho = {
        item.get("size"): (item.get("#text") or "").strip()
        for item in _como_lista(imagens)
        if isinstance(item, dict)
    }
    for tamanho in ("extralarge", "large", "medium", "small", ""):
        url = por_tamanho.get(tamanho, "")
        if url and PLACEHOLDER_HASH not in url:
            return url
    return None


class LastfmClient:
    """Cliente mínimo da API 2.0, com cache curto em memória."""

    def __init__(self, bot: LeviathanBot, api_key: str) -> None:
        self._bot = bot
        self._api_key = api_key
        self._cache: dict[tuple, tuple[float, dict[str, Any]]] = {}

    async def chamar(self, metodo: str, **params: Any) -> dict[str, Any]:
        chave = (metodo, tuple(sorted((k, str(v)) for k, v in params.items())))
        agora = time.monotonic()

        guardado = self._cache.get(chave)
        if guardado is not None and agora - guardado[0] < CACHE_TTL:
            log.debug("Cache da Last.fm: %s", metodo)
            return guardado[1]

        self._podar(agora)
        dados = await self._buscar(metodo, params)
        self._cache[chave] = (agora, dados)
        return dados

    def _podar(self, agora: float) -> None:
        vencidas = [k for k, (quando, _) in self._cache.items() if agora - quando >= CACHE_TTL]
        for chave in vencidas:
            del self._cache[chave]

    async def _buscar(self, metodo: str, params: dict[str, Any]) -> dict[str, Any]:
        sessao = self._bot.http_session
        if sessao is None:
            raise LastfmIndisponivel("O bot ainda está subindo. Tente de novo em instantes.")

        consulta = {"method": metodo, "api_key": self._api_key, "format": "json", **params}
        try:
            async with sessao.get(LASTFM_URL, params=consulta, timeout=TIMEOUT) as resposta:
                if resposta.status == 429:
                    raise LastfmIndisponivel(
                        "A Last.fm está limitando as requisições agora. "
                        "Tente de novo em instantes."
                    )
                # Erros da Last.fm vêm com status 400 e corpo JSON, então o corpo é
                # lido antes de julgar o status.
                dados = await resposta.json(content_type=None)
        except asyncio.TimeoutError as exc:
            raise LastfmIndisponivel(
                "A Last.fm demorou demais para responder. Tente de novo em instantes."
            ) from exc
        except aiohttp.ClientError as exc:
            log.warning("Falha de rede ao chamar %s: %s", metodo, exc)
            raise LastfmIndisponivel(
                "Não consegui falar com a Last.fm agora. Tente de novo em instantes."
            ) from exc

        if not isinstance(dados, dict):
            raise LastfmIndisponivel("A Last.fm devolveu uma resposta inesperada.")
        if "error" in dados:
            raise _traduzir_erro(int(dados["error"]), str(dados.get("message", "")))
        return dados


# ---------------------------------------------------------------------------
# Compatibilidade musical (função pura, testada)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ArtistaComum:
    nome: str
    plays_a: int
    plays_b: int

    @property
    def total(self) -> int:
        return self.plays_a + self.plays_b


@dataclass(frozen=True, slots=True)
class Compatibilidade:
    porcentagem: float
    comuns: tuple[ArtistaComum, ...]
    total_comuns: int


def _agregar(artistas: list[tuple[str, int]]) -> dict[str, tuple[int, str]]:
    """``nome normalizado -> (plays, nome para exibir)``, somando repetições."""
    mapa: dict[str, tuple[int, str]] = {}
    for nome, plays in artistas:
        limpo = " ".join(nome.split())
        if not limpo or plays <= 0:
            continue
        chave = limpo.casefold()
        anterior = mapa.get(chave)
        if anterior is None:
            mapa[chave] = (plays, limpo)
        else:
            mapa[chave] = (anterior[0] + plays, anterior[1])
    return mapa


def calcular_compatibilidade(
    artistas_a: list[tuple[str, int]],
    artistas_b: list[tuple[str, int]],
    *,
    destaque: int = COMPAT_DESTAQUE,
) -> Compatibilidade:
    """Compatibilidade musical entre duas listas de ``(artista, plays)``.

    A API de tasteometer da Last.fm não existe mais, então a conta é feita aqui:
    cada artista vira uma fatia do total de plays da pessoa, e a compatibilidade é
    a soma das menores fatias entre os dois (interseção de histogramas). Ponderar
    por plays evita que um artista ouvido uma vez conte igual ao favorito, e
    normalizar por total deixa a conta justa entre quem ouve muito e quem ouve
    pouco. Duas listas iguais dão 100%; sem artista em comum, 0%.
    """
    mapa_a = _agregar(artistas_a)
    mapa_b = _agregar(artistas_b)

    total_a = sum(plays for plays, _ in mapa_a.values())
    total_b = sum(plays for plays, _ in mapa_b.values())
    if not total_a or not total_b:
        return Compatibilidade(porcentagem=0.0, comuns=(), total_comuns=0)

    sobreposicao = 0.0
    comuns: list[ArtistaComum] = []
    for chave, (plays_a, nome_a) in mapa_a.items():
        par = mapa_b.get(chave)
        if par is None:
            continue
        plays_b, nome_b = par
        sobreposicao += min(plays_a / total_a, plays_b / total_b)
        comuns.append(
            ArtistaComum(
                nome=nome_a if plays_a >= plays_b else nome_b,
                plays_a=plays_a,
                plays_b=plays_b,
            )
        )

    comuns.sort(key=lambda artista: (-artista.total, artista.nome.casefold()))
    return Compatibilidade(
        porcentagem=round(sobreposicao * 100, 1),
        comuns=tuple(comuns[:destaque]),
        total_comuns=len(comuns),
    )


def barra(porcentagem: float, largura: int = 20) -> str:
    cheios = round(porcentagem / 100 * largura)
    return "█" * cheios + "░" * (largura - cheios)


# ---------------------------------------------------------------------------
# Banco
# ---------------------------------------------------------------------------


async def get_lastfm_user(db: Database, discord_id: int) -> str | None:
    row = await db.fetchone(
        "SELECT username FROM lastfm_users WHERE discord_id = ?", (discord_id,)
    )
    return row["username"] if row else None


async def set_lastfm_user(db: Database, discord_id: int, username: str) -> None:
    await db.execute(
        "INSERT INTO lastfm_users (discord_id, username) VALUES (?, ?)"
        " ON CONFLICT(discord_id) DO UPDATE SET"
        "   username = excluded.username, linked_at = datetime('now')",
        (discord_id, username),
    )


async def delete_lastfm_user(db: Database, discord_id: int) -> int:
    return await db.execute("DELETE FROM lastfm_users WHERE discord_id = ?", (discord_id,))


# ---------------------------------------------------------------------------
# Cog
# ---------------------------------------------------------------------------


def _texto(item: dict[str, Any], *caminho: str) -> str:
    """Lê um campo aninhado da resposta, devolvendo string vazia se faltar."""
    atual: Any = item
    for parte in caminho:
        if not isinstance(atual, dict):
            return ""
        atual = atual.get(parte)
    return str(atual).strip() if atual else ""


def _link(nome: str, url: str) -> str:
    seguro = discord.utils.escape_markdown(nome) or "sem nome"
    return f"[{seguro}]({url})" if url else f"**{seguro}**"


class Lastfm(commands.Cog):
    """Comandos ``/fm``."""

    fm = app_commands.Group(
        name="fm",
        description="Last.fm: o que você anda ouvindo.",
        guild_only=True,
    )

    def __init__(self, bot: LeviathanBot, api_key: str) -> None:
        self.bot = bot
        self.db = bot.db
        self.client = LastfmClient(bot, api_key)

    # -- utilidades --------------------------------------------------------

    async def _username_de(
        self, interaction: discord.Interaction, membro: discord.Member | None
    ) -> tuple[discord.abc.User, str] | None:
        """Resolve o alvo e o username. Já responde à interação se não houver."""
        alvo = membro or interaction.user
        username = await get_lastfm_user(self.db, alvo.id)
        if username is not None:
            return alvo, username

        if alvo.id == interaction.user.id:
            aviso = "Você ainda não vinculou uma conta da Last.fm. Use `/fm vincular`."
        else:
            aviso = (
                f"{alvo.display_name} ainda não vinculou uma conta da Last.fm. "
                "Só a própria pessoa pode fazer isso, com `/fm vincular`."
            )
        await interaction.response.send_message(aviso, ephemeral=True)
        return None

    @staticmethod
    async def _reportar(interaction: discord.Interaction, erro: LastfmError) -> None:
        mensagem = str(erro)
        if interaction.response.is_done():
            await interaction.followup.send(mensagem, ephemeral=True)
        else:
            await interaction.response.send_message(mensagem, ephemeral=True)

    def _autor_do_embed(self, embed: discord.Embed, alvo: discord.abc.User, username: str):
        embed.set_author(
            name=f"{alvo.display_name} · {username}",
            url=f"https://www.last.fm/user/{username}",
            icon_url=alvo.display_avatar.url,
        )
        return embed

    async def _top_artistas(self, username: str, limite: int) -> list[tuple[str, int]]:
        dados = await self.client.chamar(
            "user.getTopArtists", user=username, period="overall", limit=limite
        )
        itens = _como_lista(dados.get("topartists", {}).get("artist"))
        return [
            (str(item.get("name", "")), int(item.get("playcount", 0) or 0))
            for item in itens
        ]

    # -- vínculo -----------------------------------------------------------

    @fm.command(name="vincular", description="Liga seu Discord a um perfil da Last.fm.")
    @app_commands.describe(usuario_lastfm="Seu nome de usuário na Last.fm")
    async def fm_vincular(self, interaction: discord.Interaction, usuario_lastfm: str) -> None:
        username = usuario_lastfm.strip().lstrip("@")
        if not username:
            await interaction.response.send_message(
                "Informe um nome de usuário da Last.fm.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            # Valida antes de gravar: username errado só apareceria como erro
            # confuso no primeiro /fm tocando.
            dados = await self.client.chamar("user.getInfo", user=username)
        except LastfmError as erro:
            await self._reportar(interaction, erro)
            return

        real = _texto(dados, "user", "name") or username
        await set_lastfm_user(self.db, interaction.user.id, real)

        plays = _texto(dados, "user", "playcount")
        extra = f" ({plays} scrobbles)" if plays else ""
        await interaction.followup.send(
            f"Conta vinculada: **{real}**{extra}. Experimente `/fm tocando`.",
            ephemeral=True,
        )

    @fm.command(name="desvincular", description="Remove o vínculo com a Last.fm.")
    async def fm_desvincular(self, interaction: discord.Interaction) -> None:
        removidos = await delete_lastfm_user(self.db, interaction.user.id)
        if removidos:
            await interaction.response.send_message("Conta desvinculada.", ephemeral=True)
        else:
            await interaction.response.send_message(
                "Você não tinha nenhuma conta vinculada.", ephemeral=True
            )

    # -- tocando -----------------------------------------------------------

    @fm.command(name="tocando", description="O que está tocando agora (ou a última faixa).")
    @app_commands.describe(membro="De quem (padrão: você)")
    async def fm_tocando(
        self, interaction: discord.Interaction, membro: discord.Member | None = None
    ) -> None:
        resolvido = await self._username_de(interaction, membro)
        if resolvido is None:
            return
        alvo, username = resolvido

        await interaction.response.defer()
        try:
            dados = await self.client.chamar(
                "user.getRecentTracks", user=username, limit=1
            )
        except LastfmError as erro:
            await self._reportar(interaction, erro)
            return

        faixas = _como_lista(dados.get("recenttracks", {}).get("track"))
        if not faixas:
            await interaction.followup.send(
                f"**{username}** ainda não tem nenhum scrobble.", ephemeral=True
            )
            return

        faixa = faixas[0]
        nome = _texto(faixa, "name")
        artista = _texto(faixa, "artist", "#text") or _texto(faixa, "artist", "name")
        album = _texto(faixa, "album", "#text")
        url = _texto(faixa, "url")
        tocando_agora = str(faixa.get("@attr", {}).get("nowplaying", "")).lower() == "true"
        uts = _texto(faixa, "date", "uts")

        # Chamada extra só para o contador de plays: se falhar, o embed sai sem ele.
        plays: int | None = None
        try:
            info = await self.client.chamar(
                "track.getInfo",
                artist=artista,
                track=nome,
                username=username,
                autocorrect=1,
            )
            bruto = _texto(info, "track", "userplaycount")
            plays = int(bruto) if bruto.isdigit() else None
        except (LastfmError, ValueError):
            log.debug("Sem playcount para %s — %s", artista, nome)

        if tocando_agora:
            estado = "🎶 **Tocando agora**"
        elif uts.isdigit():
            estado = f"⏱ Tocou <t:{uts}:R>"
        else:
            estado = "⏱ Última faixa registrada"

        descricao = [estado, f"**Artista:** {discord.utils.escape_markdown(artista)}"]
        if album:
            descricao.append(f"**Álbum:** {discord.utils.escape_markdown(album)}")
        if plays is not None:
            vezes = "vez" if plays == 1 else "vezes"
            descricao.append(f"**Ouviu esta faixa:** {plays} {vezes}")

        embed = discord.Embed(
            title=nome or "sem título",
            url=url or None,
            description="\n".join(descricao),
            color=COR,
        )
        self._autor_do_embed(embed, alvo, username)
        capa = escolher_capa(faixa.get("image"))
        if capa:
            embed.set_thumbnail(url=capa)

        await interaction.followup.send(embed=embed)

    # -- recentes ----------------------------------------------------------

    @fm.command(name="recentes", description="As últimas 10 faixas tocadas.")
    @app_commands.describe(membro="De quem (padrão: você)")
    async def fm_recentes(
        self, interaction: discord.Interaction, membro: discord.Member | None = None
    ) -> None:
        resolvido = await self._username_de(interaction, membro)
        if resolvido is None:
            return
        alvo, username = resolvido

        await interaction.response.defer()
        try:
            dados = await self.client.chamar(
                "user.getRecentTracks", user=username, limit=RECENTES_LIMITE
            )
        except LastfmError as erro:
            await self._reportar(interaction, erro)
            return

        # Com uma faixa tocando agora, a API às vezes devolve um item a mais.
        faixas = _como_lista(dados.get("recenttracks", {}).get("track"))[:RECENTES_LIMITE]
        if not faixas:
            await interaction.followup.send(
                f"**{username}** ainda não tem nenhum scrobble.", ephemeral=True
            )
            return

        linhas = []
        for posicao, faixa in enumerate(faixas, start=1):
            nome = _texto(faixa, "name")
            artista = _texto(faixa, "artist", "#text") or _texto(faixa, "artist", "name")
            uts = _texto(faixa, "date", "uts")
            if str(faixa.get("@attr", {}).get("nowplaying", "")).lower() == "true":
                quando = "**tocando agora**"
            elif uts.isdigit():
                quando = f"<t:{uts}:R>"
            else:
                quando = "sem data"
            linhas.append(
                f"`{posicao:>2}.` {_link(nome, _texto(faixa, 'url'))} — "
                f"{discord.utils.escape_markdown(artista)} · {quando}"
            )

        embed = discord.Embed(
            title="Últimas faixas", description="\n".join(linhas), color=COR
        )
        self._autor_do_embed(embed, alvo, username)
        capa = escolher_capa(faixas[0].get("image"))
        if capa:
            embed.set_thumbnail(url=capa)

        await interaction.followup.send(embed=embed)

    # -- top ---------------------------------------------------------------

    @fm.command(name="top", description="Top 10 de artistas, músicas ou álbuns.")
    @app_commands.describe(
        membro="De quem (padrão: você)",
        tipo="O que ranquear (padrão: artistas)",
        periodo="Janela de tempo (padrão: sempre)",
    )
    @app_commands.choices(tipo=TIPO_CHOICES, periodo=PERIODO_CHOICES)
    async def fm_top(
        self,
        interaction: discord.Interaction,
        membro: discord.Member | None = None,
        tipo: str = "artists",
        periodo: str = "overall",
    ) -> None:
        resolvido = await self._username_de(interaction, membro)
        if resolvido is None:
            return
        alvo, username = resolvido

        rotulo, metodo, raiz, item_chave = TIPOS[tipo]

        await interaction.response.defer()
        try:
            dados = await self.client.chamar(
                metodo, user=username, period=periodo, limit=TOP_LIMITE
            )
        except LastfmError as erro:
            await self._reportar(interaction, erro)
            return

        itens = _como_lista(dados.get(raiz, {}).get(item_chave))
        if not itens:
            await interaction.followup.send(
                f"Sem dados de {rotulo} para **{username}** em {PERIODOS[periodo]}.",
                ephemeral=True,
            )
            return

        linhas = []
        for posicao, item in enumerate(itens, start=1):
            nome = _texto(item, "name")
            plays = _texto(item, "playcount") or "0"
            autoria = _texto(item, "artist", "name") or _texto(item, "artist", "#text")
            sufixo = f" — {discord.utils.escape_markdown(autoria)}" if autoria else ""
            linhas.append(
                f"`{posicao:>2}.` {_link(nome, _texto(item, 'url'))}{sufixo} "
                f"· **{plays}** plays"
            )

        embed = discord.Embed(
            title=f"Top {rotulo} · {PERIODOS[periodo]}",
            description="\n".join(linhas),
            color=COR,
        )
        self._autor_do_embed(embed, alvo, username)
        capa = escolher_capa(itens[0].get("image"))
        if capa:
            embed.set_thumbnail(url=capa)

        await interaction.followup.send(embed=embed)

    # -- compatibilidade ---------------------------------------------------

    @fm.command(name="compat", description="Compatibilidade musical entre duas pessoas.")
    @app_commands.describe(
        membro1="Primeira pessoa",
        membro2="Segunda pessoa (padrão: você)",
    )
    async def fm_compat(
        self,
        interaction: discord.Interaction,
        membro1: discord.Member,
        membro2: discord.Member | None = None,
    ) -> None:
        outro = membro2 or interaction.user
        if membro1.id == outro.id:
            await interaction.response.send_message(
                "Comparar alguém com a própria pessoa daria 100%. Escolha duas pessoas.",
                ephemeral=True,
            )
            return

        username_a = await get_lastfm_user(self.db, membro1.id)
        username_b = await get_lastfm_user(self.db, outro.id)
        faltando = [
            alvo.display_name
            for alvo, username in ((membro1, username_a), (outro, username_b))
            if username is None
        ]
        if faltando:
            await interaction.response.send_message(
                f"Sem conta vinculada: {', '.join(faltando)}. "
                "Cada pessoa precisa rodar `/fm vincular`.",
                ephemeral=True,
            )
            return
        assert username_a is not None and username_b is not None

        await interaction.response.defer()
        try:
            top_a = await self._top_artistas(username_a, COMPAT_TOP)
            top_b = await self._top_artistas(username_b, COMPAT_TOP)
        except LastfmError as erro:
            await self._reportar(interaction, erro)
            return

        if not top_a or not top_b:
            sem_dados = username_a if not top_a else username_b
            await interaction.followup.send(
                f"**{sem_dados}** ainda não tem artistas suficientes para comparar.",
                ephemeral=True,
            )
            return

        resultado = calcular_compatibilidade(top_a, top_b)

        if resultado.comuns:
            comuns = "\n".join(
                f"`{posicao}.` **{discord.utils.escape_markdown(artista.nome)}** — "
                f"{artista.plays_a} / {artista.plays_b} plays"
                for posicao, artista in enumerate(resultado.comuns, start=1)
            )
        else:
            comuns = "Nenhum artista em comum no top 50 dos dois."

        embed = discord.Embed(
            title=f"{resultado.porcentagem:.1f}% de compatibilidade",
            description=(
                f"`{barra(resultado.porcentagem)}`\n\n"
                f"**{membro1.display_name}** × **{outro.display_name}**\n"
                f"{resultado.total_comuns} artistas em comum no top {COMPAT_TOP}."
            ),
            color=COR,
        )
        embed.add_field(name="Mais ouvidos em comum", value=comuns, inline=False)
        embed.set_footer(
            text=(
                f"{username_a} × {username_b} · sobreposição ponderada por plays, "
                "desde sempre"
            )
        )
        await interaction.followup.send(embed=embed)


async def setup(bot: LeviathanBot) -> None:
    api_key = bot.config.lastfm_api_key
    if not api_key:
        raise CogUnavailable(
            "LASTFM_API_KEY não está definida no .env, então os comandos /fm ficam "
            "fora. Crie uma chave em https://www.last.fm/api/account/create"
        )
    await bot.add_cog(Lastfm(bot, api_key))
