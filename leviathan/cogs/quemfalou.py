"""Jogo "quem falou?": o bot posta uma mensagem antiga sem o autor e as pessoas
apostam em quem escreveu.

O sorteio só usa canais que o cargo ``@everyone`` consegue ver e ler. Isso não é
detalhe de conveniência: o canal do jogo é público, e sortear de um canal restrito
vazaria a mensagem para quem não deveria vê-la. Além disso, dá para excluir canais
específicos com ``/quemfalou excluir``.

A rodada sobrevive a restart porque o select é um :class:`discord.ui.DynamicItem`
com o id da rodada no ``custom_id``: o estado vive no banco e na própria mensagem,
não em memória. Rodadas vencidas durante um restart são encerradas no boot.
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands, tasks

from leviathan.bot import LeviathanBot
from leviathan.db import Database

log = logging.getLogger(__name__)

#: O jogo raciocina em horário de Brasília. Offset fixo pelo mesmo motivo do cog
#: de quotes: o Brasil não usa horário de verão desde 2019 e assim não dependemos
#: do tzdata do servidor.
FUSO = timezone(timedelta(hours=-3))

MESES = (
    "janeiro", "fevereiro", "março", "abril", "maio", "junho",
    "julho", "agosto", "setembro", "outubro", "novembro", "dezembro",
)

# -- filtros da mensagem sorteada -------------------------------------------

MIN_CARACTERES = 25
MIN_PALAVRAS = 4

URL_RE = re.compile(r"https?://\S+|www\.\S+", re.IGNORECASE)
# Prefixo de pontuação seguido de letra é quase sempre comando de bot (!play,
# /ping, .help). Duas no máximo, para pegar "--flag" sem engolir "...enfim".
COMANDO_RE = re.compile(r"^\s*[!?/.$%&+\-=~;,|^]{1,2}[A-Za-z]")

#: Quantos canais/janelas tentar antes de desistir de sortear do histórico.
TENTATIVAS_HISTORICO = 5
#: Mensagens lidas em volta do instante sorteado.
JANELA_HISTORICO = 100
#: Margem mínima de idade: nada recente demais vira rodada.
IDADE_MINIMA = timedelta(days=7)

FONTES = ("citacoes", "historico", "ambos")

FONTE_CHOICES = [
    app_commands.Choice(name="citações salvas", value="citacoes"),
    app_commands.Choice(name="histórico dos canais", value="historico"),
    app_commands.Choice(name="ambos", value="ambos"),
]

#: De quanto em quanto tempo o agendador reavalia. Não é o intervalo entre
#: rodadas: esse vem da config e é comparado contra a última rodada, para que
#: mudar a config valha na hora, sem reiniciar o loop.
TICK_AGENDADOR = timedelta(minutes=5)
#: Com que frequência rodadas vencidas são encerradas.
TICK_EXPIRACAO = timedelta(seconds=30)

COOLDOWN_AGORA = 30 * 60


# ---------------------------------------------------------------------------
# Lógica pura
# ---------------------------------------------------------------------------


def texto_elegivel(conteudo: str) -> bool:
    """Se o texto tem substância suficiente para virar rodada.

    Os links são removidos antes de medir, o que resolve os três critérios de uma
    vez: "pelo menos 25 caracteres", "pelo menos 4 palavras" e "não é só link" —
    uma mensagem que só tem URL sobra vazia e é rejeitada.
    """
    if COMANDO_RE.match(conteudo):
        return False
    limpo = " ".join(URL_RE.sub(" ", conteudo).split())
    if len(limpo) < MIN_CARACTERES:
        return False
    return len(limpo.split()) >= MIN_PALAVRAS


def mensagem_elegivel(
    *,
    conteudo: str,
    autor_e_bot: bool,
    autor_no_servidor: bool,
    channel_id: int,
    canal_do_jogo: int,
) -> bool:
    """Todos os filtros de uma mensagem candidata.

    O autor precisa continuar no servidor: se saiu, ninguém consegue acertar e a
    rodada só queima uma mensagem boa.
    """
    if autor_e_bot or not autor_no_servidor:
        return False
    if channel_id == canal_do_jogo:
        return False
    return texto_elegivel(conteudo)


def dentro_do_silencio(hora: int, inicio: int, fim: int) -> bool:
    """Se ``hora`` cai na janela de silêncio ``[inicio, fim)``.

    A janela pode virar a meia-noite (22h às 6h, por exemplo), caso em que o
    intervalo é a união de ``[inicio, 24)`` com ``[0, fim)``.
    """
    if inicio == fim:
        return False  # janela vazia: nunca silencia
    if inicio < fim:
        return inicio <= hora < fim
    return hora >= inicio or hora < fim


def data_aproximada(quando: datetime) -> str:
    """``março de 2025`` — vago de propósito, para não entregar a mensagem."""
    if quando.tzinfo is None:
        quando = quando.replace(tzinfo=timezone.utc)
    local = quando.astimezone(FUSO)
    return f"{MESES[local.month - 1]} de {local.year}"


async def houve_movimento(
    ultima_rodada: datetime | None,
    *,
    tem_palpite: Callable[[], Awaitable[bool]],
    tem_mensagem: Callable[[], Awaitable[bool]],
) -> bool:
    """Se o jogo deu sinal de vida desde a última rodada.

    Palpitar é uma interação com o select, não uma mensagem: num canal dedicado
    ao jogo as pessoas jogam sem escrever nada, e olhar só o histórico faria o
    agendador nunca mais disparar. Por isso o palpite conta como movimento.

    O palpite é checado primeiro porque é só uma consulta ao banco; o histórico
    do canal, que custa uma chamada à API, só é lido se não houver palpite.
    """
    if ultima_rodada is None:
        return True
    if await tem_palpite():
        return True
    return await tem_mensagem()


def agora_utc() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Banco
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Config:
    guild_id: int
    channel_id: int
    intervalo_horas: float
    pontos: int
    fonte: str
    minutos_rodada: int
    silencio_inicio: int
    silencio_fim: int
    pausado: bool
    ultima_rodada: datetime | None


@dataclass(frozen=True, slots=True)
class Rodada:
    id: int
    guild_id: int
    game_channel_id: int
    game_message_id: int | None
    origem_channel_id: int
    origem_message_id: int
    autor_id: int
    conteudo: str
    origem_criada_em: datetime
    jump_url: str
    status: str
    vencedor_id: int | None
    expira_em: datetime


def _data(valor: str | None) -> datetime | None:
    return datetime.fromisoformat(valor) if valor else None


def _config_from_row(row) -> Config:
    return Config(
        guild_id=row["guild_id"],
        channel_id=row["channel_id"],
        intervalo_horas=float(row["intervalo_horas"]),
        pontos=int(row["pontos"]),
        fonte=row["fonte"],
        minutos_rodada=int(row["minutos_rodada"]),
        silencio_inicio=int(row["silencio_inicio"]),
        silencio_fim=int(row["silencio_fim"]),
        pausado=bool(row["pausado"]),
        ultima_rodada=_data(row["ultima_rodada"]),
    )


async def get_config(db: Database, guild_id: int) -> Config | None:
    row = await db.fetchone(
        "SELECT * FROM quemfalou_config WHERE guild_id = ?", (guild_id,)
    )
    return _config_from_row(row) if row else None


async def listar_configs(db: Database) -> list[Config]:
    rows = await db.fetchall("SELECT * FROM quemfalou_config")
    return [_config_from_row(row) for row in rows]


async def salvar_config(db: Database, guild_id: int, **campos: Any) -> None:
    """Cria ou atualiza a config, mexendo só nos campos informados."""
    atual = await get_config(db, guild_id)
    if atual is None:
        if "channel_id" not in campos:
            raise ValueError("a primeira configuração precisa de channel_id")
        colunas = ["guild_id", *campos]
        marcas = ", ".join("?" for _ in colunas)
        await db.execute(
            f"INSERT INTO quemfalou_config ({', '.join(colunas)}) VALUES ({marcas})",
            (guild_id, *campos.values()),
        )
        return

    if not campos:
        return
    atribuicoes = ", ".join(f"{coluna} = ?" for coluna in campos)
    await db.execute(
        f"UPDATE quemfalou_config SET {atribuicoes}, updated_at = datetime('now')"
        " WHERE guild_id = ?",
        (*campos.values(), guild_id),
    )


async def canais_excluidos(db: Database, guild_id: int) -> set[int]:
    rows = await db.fetchall(
        "SELECT channel_id FROM quemfalou_excluidos WHERE guild_id = ?", (guild_id,)
    )
    return {row["channel_id"] for row in rows}


async def excluir_canal(db: Database, guild_id: int, channel_id: int) -> bool:
    afetadas = await db.execute(
        "INSERT OR IGNORE INTO quemfalou_excluidos (guild_id, channel_id) VALUES (?, ?)",
        (guild_id, channel_id),
    )
    return afetadas > 0


async def incluir_canal(db: Database, guild_id: int, channel_id: int) -> bool:
    afetadas = await db.execute(
        "DELETE FROM quemfalou_excluidos WHERE guild_id = ? AND channel_id = ?",
        (guild_id, channel_id),
    )
    return afetadas > 0


def _rodada_from_row(row) -> Rodada:
    return Rodada(
        id=row["id"],
        guild_id=row["guild_id"],
        game_channel_id=row["game_channel_id"],
        game_message_id=row["game_message_id"],
        origem_channel_id=row["origem_channel_id"],
        origem_message_id=row["origem_message_id"],
        autor_id=row["autor_id"],
        conteudo=row["conteudo"],
        origem_criada_em=datetime.fromisoformat(row["origem_criada_em"]),
        jump_url=row["jump_url"],
        status=row["status"],
        vencedor_id=row["vencedor_id"],
        expira_em=datetime.fromisoformat(row["expira_em"]),
    )


async def get_rodada(db: Database, rodada_id: int) -> Rodada | None:
    row = await db.fetchone("SELECT * FROM quemfalou_rodadas WHERE id = ?", (rodada_id,))
    return _rodada_from_row(row) if row else None


async def rodada_ativa(db: Database, guild_id: int) -> Rodada | None:
    row = await db.fetchone(
        "SELECT * FROM quemfalou_rodadas WHERE guild_id = ? AND status = 'ativa'"
        " ORDER BY id DESC LIMIT 1",
        (guild_id,),
    )
    return _rodada_from_row(row) if row else None


async def rodadas_vencidas(db: Database) -> list[Rodada]:
    rows = await db.fetchall(
        "SELECT * FROM quemfalou_rodadas WHERE status = 'ativa' AND expira_em <= ?",
        (agora_utc().isoformat(),),
    )
    return [_rodada_from_row(row) for row in rows]


async def mensagens_usadas(db: Database, guild_id: int) -> set[int]:
    rows = await db.fetchall(
        "SELECT origem_message_id FROM quemfalou_rodadas WHERE guild_id = ?", (guild_id,)
    )
    return {row["origem_message_id"] for row in rows}


async def criar_rodada(
    db: Database,
    *,
    guild_id: int,
    game_channel_id: int,
    origem_channel_id: int,
    origem_message_id: int,
    autor_id: int,
    conteudo: str,
    origem_criada_em: datetime,
    jump_url: str,
    expira_em: datetime,
) -> Rodada | None:
    """Cria a rodada. ``None`` se a mensagem já tinha sido usada antes."""
    afetadas = await db.execute(
        "INSERT OR IGNORE INTO quemfalou_rodadas"
        " (guild_id, game_channel_id, origem_channel_id, origem_message_id, autor_id,"
        "  conteudo, origem_criada_em, jump_url, expira_em)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            guild_id,
            game_channel_id,
            origem_channel_id,
            origem_message_id,
            autor_id,
            conteudo,
            origem_criada_em.isoformat(),
            jump_url,
            expira_em.isoformat(),
        ),
    )
    if not afetadas:
        return None
    row = await db.fetchone(
        "SELECT * FROM quemfalou_rodadas WHERE guild_id = ? AND origem_message_id = ?",
        (guild_id, origem_message_id),
    )
    return _rodada_from_row(row) if row else None


async def set_game_message(db: Database, rodada_id: int, message_id: int) -> None:
    await db.execute(
        "UPDATE quemfalou_rodadas SET game_message_id = ? WHERE id = ?",
        (message_id, rodada_id),
    )


async def registrar_palpite(
    db: Database, rodada_id: int, usuario_id: int, palpite_id: int, acertou: bool
) -> bool:
    """Registra o palpite. ``False`` se a pessoa já tinha palpitado nesta rodada.

    A chave primária ``(rodada_id, usuario_id)`` é a regra de um palpite por
    pessoa: o ``INSERT OR IGNORE`` que não insere nada devolve ``rowcount == 0``.
    """
    afetadas = await db.execute(
        "INSERT OR IGNORE INTO quemfalou_palpites"
        " (rodada_id, usuario_id, palpite_id, acertou) VALUES (?, ?, ?, ?)",
        (rodada_id, usuario_id, palpite_id, int(acertou)),
    )
    return afetadas > 0


async def contar_palpites(db: Database, rodada_id: int) -> tuple[int, int]:
    """``(total, errados)`` da rodada."""
    row = await db.fetchone(
        "SELECT COUNT(*) AS total, SUM(1 - acertou) AS errados"
        " FROM quemfalou_palpites WHERE rodada_id = ?",
        (rodada_id,),
    )
    if row is None:
        return 0, 0
    return int(row["total"] or 0), int(row["errados"] or 0)


async def houve_palpite_na_ultima_rodada(db: Database, guild_id: int) -> bool:
    """Se alguém palpitou na rodada mais recente da guild."""
    row = await db.fetchone(
        "SELECT 1 FROM quemfalou_palpites WHERE rodada_id ="
        " (SELECT id FROM quemfalou_rodadas WHERE guild_id = ? ORDER BY id DESC LIMIT 1)"
        " LIMIT 1",
        (guild_id,),
    )
    return row is not None


async def marcar_acerto(db: Database, rodada_id: int, vencedor_id: int) -> bool:
    """Fecha a rodada como acertada. ``False`` se outra pessoa chegou primeiro.

    O ``WHERE status = 'ativa'`` é o desempate: dois palpites certos simultâneos
    disputam esse UPDATE e só um altera linha.
    """
    afetadas = await db.execute(
        "UPDATE quemfalou_rodadas"
        " SET status = 'acertada', vencedor_id = ?, encerrada_em = datetime('now')"
        " WHERE id = ? AND status = 'ativa'",
        (vencedor_id, rodada_id),
    )
    return afetadas > 0


async def marcar_expirada(db: Database, rodada_id: int) -> bool:
    afetadas = await db.execute(
        "UPDATE quemfalou_rodadas"
        " SET status = 'expirada', encerrada_em = datetime('now')"
        " WHERE id = ? AND status = 'ativa'",
        (rodada_id,),
    )
    return afetadas > 0


async def somar_placar(
    db: Database, guild_id: int, usuario_id: int, *, pontos: int, acertou: bool
) -> None:
    await db.execute(
        "INSERT INTO quemfalou_placar (guild_id, usuario_id, pontos, acertos, palpites)"
        " VALUES (?, ?, ?, ?, 1)"
        " ON CONFLICT(guild_id, usuario_id) DO UPDATE SET"
        "   pontos = pontos + excluded.pontos,"
        "   acertos = acertos + excluded.acertos,"
        "   palpites = palpites + 1",
        (guild_id, usuario_id, pontos, int(acertou)),
    )


async def placar(db: Database, guild_id: int, limite: int) -> list[tuple[int, int, int, int]]:
    """``[(usuario_id, pontos, acertos, palpites)]`` do maior para o menor."""
    rows = await db.fetchall(
        "SELECT usuario_id, pontos, acertos, palpites FROM quemfalou_placar"
        " WHERE guild_id = ? ORDER BY pontos DESC, acertos DESC, usuario_id ASC"
        " LIMIT ?",
        (guild_id, limite),
    )
    return [
        (int(r["usuario_id"]), int(r["pontos"]), int(r["acertos"]), int(r["palpites"]))
        for r in rows
    ]


#: Tamanho da amostra sorteada da tabela ``quotes``.
AMOSTRA_CITACOES = 50


async def citacoes_sorteadas(
    db: Database, guild_id: int, limite: int = AMOSTRA_CITACOES
) -> list[dict[str, Any]]:
    """Amostra da tabela ``quotes``, que já guarda o ``clean_content``.

    Devolve a amostra inteira em vez de uma citação só: os filtros rodam depois,
    e desistir na primeira reprovada jogaria fora as outras quarenta e nove.
    """
    rows = await db.fetchall(
        "SELECT message_id, channel_id, author_id, content, created_at, jump_url"
        " FROM quotes WHERE guild_id = ? ORDER BY RANDOM() LIMIT ?",
        (guild_id, limite),
    )
    return [dict(row) for row in rows]


def escolher_citacao(
    citacoes: Iterable[dict[str, Any]],
    *,
    usadas: set[int],
    canais_permitidos: set[int],
    autor_no_servidor: Callable[[int], bool],
) -> dict[str, Any] | None:
    """Primeira citação da amostra que passa em TODOS os filtros.

    A tabela ``quotes`` guarda citação de qualquer canal e não sabe nada de
    privacidade: alguém pode ter fixado uma mensagem de canal restrito. Por isso
    ``canais_permitidos`` — o resultado de :meth:`QuemFalou.canais_sorteaveis` —
    vale aqui igualzinho ao sorteio do histórico. Citação de canal privado, de
    canal excluído, do próprio canal do jogo ou de canal que nem existe mais
    simplesmente não está nesse conjunto.
    """
    for citacao in citacoes:
        if citacao["message_id"] in usadas:
            continue
        if citacao["channel_id"] not in canais_permitidos:
            continue
        if not autor_no_servidor(citacao["author_id"]):
            continue
        if not texto_elegivel(citacao["content"]):
            continue
        return citacao
    return None


# ---------------------------------------------------------------------------
# Componente persistente
# ---------------------------------------------------------------------------

TEMPLATE_PALPITE = r"quemfalou:rodada:(?P<rodada>\d+)"


class PalpiteSelect(
    discord.ui.DynamicItem[discord.ui.UserSelect],
    template=TEMPLATE_PALPITE,
):
    """Select de membro com o id da rodada embutido no ``custom_id``.

    Por ser dinâmico, nada precisa ficar em memória entre restarts: o discord.py
    reconstrói o item a partir do ``custom_id`` da mensagem quando alguém
    interage, e o estado real da rodada é lido do banco.
    """

    def __init__(self, rodada_id: int) -> None:
        self.rodada_id = rodada_id
        super().__init__(
            discord.ui.UserSelect(
                custom_id=f"quemfalou:rodada:{rodada_id}",
                placeholder="Quem você acha que escreveu isso?",
                min_values=1,
                max_values=1,
            )
        )

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):  # type: ignore[override]
        return cls(int(match["rodada"]))

    async def callback(self, interaction: discord.Interaction) -> None:  # type: ignore[override]
        cog = interaction.client.get_cog("QuemFalou")
        if cog is None:
            await interaction.response.send_message(
                "O jogo está indisponível agora.", ephemeral=True
            )
            return
        escolhidos = self.item.values
        if not escolhidos:
            await interaction.response.send_message(
                "Escolha alguém para palpitar.", ephemeral=True
            )
            return
        await cog.processar_palpite(interaction, self.rodada_id, escolhidos[0].id)


def view_da_rodada(rodada_id: int) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(PalpiteSelect(rodada_id))
    return view


def view_encerrada(rodada_id: int) -> discord.ui.View:
    """Mesmo select, desabilitado e com custom_id fora do template."""
    view = discord.ui.View(timeout=None)
    view.add_item(
        discord.ui.UserSelect(
            custom_id=f"quemfalou:encerrada:{rodada_id}",
            placeholder="Rodada encerrada",
            disabled=True,
        )
    )
    return view


# ---------------------------------------------------------------------------
# Cog
# ---------------------------------------------------------------------------


def _cooldown_agora(interaction: discord.Interaction) -> app_commands.Cooldown | None:
    """Sem cooldown para quem gerencia o servidor; 30 min para o resto."""
    usuario = interaction.user
    if isinstance(usuario, discord.Member) and usuario.guild_permissions.manage_guild:
        return None
    return app_commands.Cooldown(1, COOLDOWN_AGORA)


class QuemFalou(commands.Cog):
    """O jogo de adivinhar quem escreveu uma mensagem antiga."""

    quemfalou = app_commands.Group(
        name="quemfalou",
        description="Jogo de adivinhar o autor de mensagens antigas.",
        guild_only=True,
    )

    def __init__(self, bot: LeviathanBot) -> None:
        self.bot = bot
        self.db = bot.db
        # Uma rodada por guild de cada vez; o lock evita que o agendador e o
        # /quemfalou agora criem duas ao mesmo tempo.
        self._locks: dict[int, asyncio.Lock] = {}

    async def cog_load(self) -> None:
        self.bot.add_dynamic_items(PalpiteSelect)
        self.verificar_expiradas.start()
        self.agendador.start()

    async def cog_unload(self) -> None:
        self.agendador.cancel()
        self.verificar_expiradas.cancel()
        self.bot.remove_dynamic_items(PalpiteSelect)

    def _lock(self, guild_id: int) -> asyncio.Lock:
        lock = self._locks.get(guild_id)
        if lock is None:
            lock = self._locks[guild_id] = asyncio.Lock()
        return lock

    # -- laços -------------------------------------------------------------

    @tasks.loop(seconds=TICK_EXPIRACAO.total_seconds())
    async def verificar_expiradas(self) -> None:
        """Encerra rodadas cujo prazo venceu, inclusive durante um restart."""
        for rodada in await rodadas_vencidas(self.db):
            try:
                await self.encerrar(rodada, vencedor_id=None)
            except Exception:
                log.exception("Falha ao encerrar a rodada %d", rodada.id)

    @verificar_expiradas.before_loop
    async def _antes_expiradas(self) -> None:
        await self.bot.wait_until_ready()

    @tasks.loop(seconds=TICK_AGENDADOR.total_seconds())
    async def agendador(self) -> None:
        """Reavalia, a cada tick, se alguma guild já pode receber rodada nova.

        O tick é fixo e o intervalo real vem da config, comparado contra a última
        rodada. Assim, mudar ``intervalo_horas`` vale na hora, sem reiniciar loop.
        """
        for config in await listar_configs(self.db):
            try:
                if await self._deve_disparar(config):
                    await self.iniciar_rodada(config.guild_id)
            except Exception:
                log.exception("Falha no agendador da guild %d", config.guild_id)

    @agendador.before_loop
    async def _antes_agendador(self) -> None:
        await self.bot.wait_until_ready()

    async def _deve_disparar(self, config: Config) -> bool:
        if config.pausado:
            return False

        hora = agora_utc().astimezone(FUSO).hour
        if dentro_do_silencio(hora, config.silencio_inicio, config.silencio_fim):
            return False

        if config.ultima_rodada is not None:
            proxima = config.ultima_rodada + timedelta(hours=config.intervalo_horas)
            if agora_utc() < proxima:
                return False

        if await rodada_ativa(self.db, config.guild_id) is not None:
            return False

        return await self._canal_teve_movimento(config)

    async def _canal_teve_movimento(self, config: Config) -> bool:
        """Se houve palpite na última rodada ou mensagem humana no canal.

        Sem isso, um servidor parado receberia rodada atrás de rodada sem ninguém
        para jogar. A checagem em si está em :func:`houve_movimento`, que também
        garante a ordem: banco primeiro, API só se necessário.
        """
        return await houve_movimento(
            config.ultima_rodada,
            tem_palpite=lambda: houve_palpite_na_ultima_rodada(self.db, config.guild_id),
            tem_mensagem=lambda: self._mensagem_humana_no_canal(config),
        )

    async def _mensagem_humana_no_canal(self, config: Config) -> bool:
        """Se alguém humano falou no canal do jogo desde a última rodada."""
        assert config.ultima_rodada is not None
        canal = self.bot.get_channel(config.channel_id)
        if not isinstance(canal, discord.TextChannel):
            return False
        try:
            async for mensagem in canal.history(after=config.ultima_rodada, limit=50):
                if not mensagem.author.bot:
                    return True
        except (discord.Forbidden, discord.HTTPException):
            log.warning("Não consegui ler o canal do jogo %d", config.channel_id)
            return False
        return False

    # -- sorteio -----------------------------------------------------------

    def canais_sorteaveis(
        self, guild: discord.Guild, *, excluidos: set[int], canal_do_jogo: int
    ) -> list[discord.TextChannel]:
        """Canais públicos de verdade: ``@everyone`` vê e lê o histórico.

        Esta é a barreira de privacidade do jogo. Um canal restrito jamais entra,
        porque o canal do jogo é público e a mensagem seria exposta a quem não
        tinha acesso a ela.
        """
        everyone = guild.default_role
        eu = guild.me
        elegiveis = []
        for canal in guild.text_channels:
            if canal.id in excluidos or canal.id == canal_do_jogo:
                continue
            publico = canal.permissions_for(everyone)
            if not (publico.view_channel and publico.read_message_history):
                continue
            if eu is not None:
                minhas = canal.permissions_for(eu)
                if not (minhas.view_channel and minhas.read_message_history):
                    continue
            elegiveis.append(canal)
        return elegiveis

    async def sortear_do_historico(
        self,
        guild: discord.Guild,
        config: Config,
        usadas: set[int],
        canais: list[discord.TextChannel],
    ) -> discord.Message | None:
        if not canais:
            log.info("Guild %d não tem canal público sorteável", guild.id)
            return None

        limite = agora_utc() - IDADE_MINIMA
        for _ in range(TENTATIVAS_HISTORICO):
            canal = random.choice(canais)
            inicio = canal.created_at
            if limite <= inicio:
                continue
            # Instante aleatório na vida do canal, convertido em snowflake: é o
            # jeito de pedir "mensagens em volta desta data" sem paginar tudo.
            instante = inicio + (limite - inicio) * random.random()
            referencia = discord.Object(id=discord.utils.time_snowflake(instante))

            try:
                candidatas = [
                    mensagem
                    async for mensagem in canal.history(
                        around=referencia, limit=JANELA_HISTORICO
                    )
                    if mensagem.id not in usadas
                    and mensagem_elegivel(
                        conteudo=mensagem.clean_content,
                        autor_e_bot=mensagem.author.bot,
                        autor_no_servidor=guild.get_member(mensagem.author.id) is not None,
                        channel_id=canal.id,
                        canal_do_jogo=config.channel_id,
                    )
                ]
            except (discord.Forbidden, discord.HTTPException) as exc:
                log.warning("Não consegui ler o histórico de #%s: %s", canal.name, exc)
                continue

            if candidatas:
                return random.choice(candidatas)

        log.info("Nenhuma mensagem elegível após %d tentativas", TENTATIVAS_HISTORICO)
        return None

    async def sortear(
        self, guild: discord.Guild, config: Config
    ) -> dict[str, Any] | None:
        """Escolhe a mensagem da rodada conforme a fonte configurada."""
        usadas = await mensagens_usadas(self.db, guild.id)
        excluidos = await canais_excluidos(self.db, guild.id)
        # A mesma barreira de privacidade para as duas fontes.
        canais = self.canais_sorteaveis(
            guild, excluidos=excluidos, canal_do_jogo=config.channel_id
        )
        permitidos = {canal.id for canal in canais}

        fontes = ["citacoes", "historico"] if config.fonte == "ambos" else [config.fonte]
        random.shuffle(fontes)

        for fonte in fontes:
            if fonte == "citacoes":
                citacao = escolher_citacao(
                    await citacoes_sorteadas(self.db, guild.id),
                    usadas=usadas,
                    canais_permitidos=permitidos,
                    autor_no_servidor=lambda autor: guild.get_member(autor) is not None,
                )
                if citacao is None:
                    continue
                return {
                    "origem_channel_id": citacao["channel_id"],
                    "origem_message_id": citacao["message_id"],
                    "autor_id": citacao["author_id"],
                    "conteudo": citacao["content"],
                    "criada_em": datetime.fromisoformat(citacao["created_at"]),
                    "jump_url": citacao["jump_url"],
                }

            mensagem = await self.sortear_do_historico(guild, config, usadas, canais)
            if mensagem is not None:
                return {
                    "origem_channel_id": mensagem.channel.id,
                    "origem_message_id": mensagem.id,
                    "autor_id": mensagem.author.id,
                    "conteudo": mensagem.clean_content,
                    "criada_em": mensagem.created_at,
                    "jump_url": mensagem.jump_url,
                }
        return None

    # -- rodada ------------------------------------------------------------

    def embed_da_rodada(self, rodada: Rodada, canal_origem: str) -> discord.Embed:
        embed = discord.Embed(
            title="Quem falou?",
            description=f">>> {rodada.conteudo}",
            color=discord.Color.blurple(),
        )
        embed.add_field(name="Canal", value=f"#{canal_origem}", inline=True)
        embed.add_field(
            name="Quando", value=data_aproximada(rodada.origem_criada_em), inline=True
        )
        embed.set_footer(
            text="Um palpite por pessoa. Quem acertar primeiro leva os pontos."
        )
        return embed

    async def iniciar_rodada(self, guild_id: int, *, forcar: bool = False) -> Rodada | None:
        """Sorteia e publica uma rodada. ``None`` se não deu para começar."""
        async with self._lock(guild_id):
            config = await get_config(self.db, guild_id)
            if config is None:
                return None
            if not forcar and config.pausado:
                return None
            if await rodada_ativa(self.db, guild_id) is not None:
                return None

            guild = self.bot.get_guild(guild_id)
            if guild is None:
                return None
            canal = guild.get_channel(config.channel_id)
            if not isinstance(canal, discord.TextChannel):
                log.warning("Canal do jogo %d inacessível", config.channel_id)
                return None

            escolhida = await self.sortear(guild, config)
            if escolhida is None:
                return None

            rodada = await criar_rodada(
                self.db,
                guild_id=guild_id,
                game_channel_id=canal.id,
                origem_channel_id=escolhida["origem_channel_id"],
                origem_message_id=escolhida["origem_message_id"],
                autor_id=escolhida["autor_id"],
                conteudo=escolhida["conteudo"],
                origem_criada_em=escolhida["criada_em"],
                jump_url=escolhida["jump_url"],
                expira_em=agora_utc() + timedelta(minutes=config.minutos_rodada),
            )
            if rodada is None:
                return None

            canal_origem = guild.get_channel(escolhida["origem_channel_id"])
            nome_origem = getattr(canal_origem, "name", "desconhecido")

            try:
                mensagem = await canal.send(
                    embed=self.embed_da_rodada(rodada, nome_origem),
                    view=view_da_rodada(rodada.id),
                )
            except (discord.Forbidden, discord.HTTPException):
                log.exception("Falha ao publicar a rodada %d", rodada.id)
                await marcar_expirada(self.db, rodada.id)
                return None

            await set_game_message(self.db, rodada.id, mensagem.id)
            await salvar_config(
                self.db, guild_id, ultima_rodada=agora_utc().isoformat()
            )
            log.info("Rodada %d publicada na guild %d", rodada.id, guild_id)
            return rodada

    async def encerrar(self, rodada: Rodada, *, vencedor_id: int | None) -> None:
        """Revela o autor, edita a mensagem e desativa o select."""
        if vencedor_id is None and not await marcar_expirada(self.db, rodada.id):
            return  # alguém acertou entre a leitura e agora

        total, errados = await contar_palpites(self.db, rodada.id)
        guild = self.bot.get_guild(rodada.guild_id)

        embed = discord.Embed(
            title="Quem falou?",
            description=f">>> {rodada.conteudo}",
            color=discord.Color.green() if vencedor_id else discord.Color.dark_grey(),
        )
        embed.add_field(name="Era", value=f"<@{rodada.autor_id}>", inline=True)
        embed.add_field(
            name="Quando", value=data_aproximada(rodada.origem_criada_em), inline=True
        )
        embed.add_field(
            name="Mensagem original",
            value=f"[ir até ela]({rodada.jump_url})",
            inline=True,
        )
        if vencedor_id is not None:
            embed.add_field(name="Acertou", value=f"<@{vencedor_id}>", inline=False)
        else:
            embed.add_field(name="Acertou", value="Ninguém. O tempo acabou.", inline=False)
        embed.set_footer(
            text=f"{total} palpite(s), {errados} errado(s)."
        )

        if guild is None or rodada.game_message_id is None:
            return
        canal = guild.get_channel(rodada.game_channel_id)
        if not isinstance(canal, discord.TextChannel):
            return
        try:
            mensagem = await canal.fetch_message(rodada.game_message_id)
            await mensagem.edit(embed=embed, view=view_encerrada(rodada.id))
        except discord.NotFound:
            log.info("Mensagem da rodada %d sumiu antes do encerramento", rodada.id)
        except (discord.Forbidden, discord.HTTPException):
            log.exception("Falha ao encerrar visualmente a rodada %d", rodada.id)

    async def processar_palpite(
        self, interaction: discord.Interaction, rodada_id: int, palpite_id: int
    ) -> None:
        rodada = await get_rodada(self.db, rodada_id)
        if rodada is None or rodada.status != "ativa":
            await interaction.response.send_message(
                "Essa rodada já encerrou.", ephemeral=True
            )
            return

        if interaction.user.id == rodada.autor_id:
            await interaction.response.send_message(
                "Essa é sua, espertinho. Deixa os outros adivinharem.", ephemeral=True
            )
            return

        acertou = palpite_id == rodada.autor_id
        novo = await registrar_palpite(
            self.db, rodada_id, interaction.user.id, palpite_id, acertou
        )
        if not novo:
            await interaction.response.send_message(
                "Você já palpitou nesta rodada. É um por pessoa.", ephemeral=True
            )
            return

        config = await get_config(self.db, rodada.guild_id)
        pontos = config.pontos if (config and acertou) else 0

        if not acertou:
            await somar_placar(
                self.db, rodada.guild_id, interaction.user.id, pontos=0, acertou=False
            )
            await interaction.response.send_message(
                f"Não foi <@{palpite_id}>. Boa tentativa.", ephemeral=True
            )
            return

        # O UPDATE condicional decide quem foi o primeiro.
        venceu = await marcar_acerto(self.db, rodada_id, interaction.user.id)
        await somar_placar(
            self.db,
            rodada.guild_id,
            interaction.user.id,
            pontos=pontos if venceu else 0,
            acertou=True,
        )
        if not venceu:
            await interaction.response.send_message(
                "Você acertou, mas alguém chegou primeiro.", ephemeral=True
            )
            return

        await interaction.response.send_message(
            f"Acertou! Era <@{rodada.autor_id}>. +{pontos} pontos.", ephemeral=True
        )
        atualizada = await get_rodada(self.db, rodada_id)
        if atualizada is not None:
            await self.encerrar(atualizada, vencedor_id=interaction.user.id)

    # -- comandos ----------------------------------------------------------

    @quemfalou.command(name="config", description="Configura o jogo neste servidor.")
    @app_commands.describe(
        canal="Canal onde as rodadas são postadas",
        intervalo_horas="Horas entre rodadas (padrão: 6)",
        pontos="Pontos por acerto (padrão: 10)",
        fonte="De onde vêm as mensagens (padrão: ambos)",
        minutos_rodada="Minutos até a rodada expirar (padrão: 10)",
    )
    @app_commands.choices(fonte=FONTE_CHOICES)
    @app_commands.checks.has_permissions(manage_guild=True)
    async def cmd_config(
        self,
        interaction: discord.Interaction,
        canal: discord.TextChannel,
        intervalo_horas: app_commands.Range[float, 0.25, 168.0] | None = None,
        pontos: app_commands.Range[int, 1, 1000] | None = None,
        fonte: str | None = None,
        minutos_rodada: app_commands.Range[int, 1, 1440] | None = None,
    ) -> None:
        assert interaction.guild_id is not None

        permissoes = canal.permissions_for(canal.guild.me)
        if not (permissoes.send_messages and permissoes.embed_links):
            await interaction.response.send_message(
                f"Não consigo postar em {canal.mention}: preciso de "
                "'Enviar mensagens' e 'Inserir links'.",
                ephemeral=True,
            )
            return

        campos: dict[str, Any] = {"channel_id": canal.id}
        if intervalo_horas is not None:
            campos["intervalo_horas"] = float(intervalo_horas)
        if pontos is not None:
            campos["pontos"] = int(pontos)
        if fonte is not None:
            campos["fonte"] = fonte
        if minutos_rodada is not None:
            campos["minutos_rodada"] = int(minutos_rodada)

        await salvar_config(self.db, interaction.guild_id, **campos)
        config = await get_config(self.db, interaction.guild_id)
        assert config is not None

        embed = discord.Embed(title="Quem falou — configuração", color=discord.Color.blurple())
        embed.add_field(name="Canal", value=canal.mention, inline=True)
        embed.add_field(name="Intervalo", value=f"{config.intervalo_horas:g}h", inline=True)
        embed.add_field(name="Pontos", value=str(config.pontos), inline=True)
        embed.add_field(name="Fonte", value=config.fonte, inline=True)
        embed.add_field(name="Duração", value=f"{config.minutos_rodada} min", inline=True)
        embed.add_field(
            name="Silêncio",
            value=f"{config.silencio_inicio}h às {config.silencio_fim}h (Brasília)",
            inline=True,
        )
        embed.set_footer(text="Só canais que o @everyone consegue ler entram no sorteio.")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @quemfalou.command(name="excluir", description="Nunca sortear deste canal.")
    @app_commands.describe(canal="Canal a excluir do sorteio")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def cmd_excluir(
        self, interaction: discord.Interaction, canal: discord.TextChannel
    ) -> None:
        assert interaction.guild_id is not None
        novo = await excluir_canal(self.db, interaction.guild_id, canal.id)
        await interaction.response.send_message(
            f"{canal.mention} {'excluído do' if novo else 'já estava fora do'} sorteio.",
            ephemeral=True,
        )

    @quemfalou.command(name="incluir", description="Volta a sortear deste canal.")
    @app_commands.describe(canal="Canal a devolver ao sorteio")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def cmd_incluir(
        self, interaction: discord.Interaction, canal: discord.TextChannel
    ) -> None:
        assert interaction.guild_id is not None
        removido = await incluir_canal(self.db, interaction.guild_id, canal.id)
        await interaction.response.send_message(
            f"{canal.mention} {'voltou ao' if removido else 'já estava no'} sorteio."
            " Ele só entra se o @everyone conseguir lê-lo.",
            ephemeral=True,
        )

    @quemfalou.command(name="agora", description="Dispara uma rodada agora.")
    @app_commands.checks.dynamic_cooldown(_cooldown_agora)
    async def cmd_agora(self, interaction: discord.Interaction) -> None:
        assert interaction.guild_id is not None

        config = await get_config(self.db, interaction.guild_id)
        if config is None:
            await interaction.response.send_message(
                "O jogo ainda não foi configurado. Use `/quemfalou config`.",
                ephemeral=True,
            )
            return
        if await rodada_ativa(self.db, interaction.guild_id) is not None:
            await interaction.response.send_message(
                "Já tem uma rodada rolando.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        rodada = await self.iniciar_rodada(interaction.guild_id, forcar=True)
        if rodada is None:
            await interaction.followup.send(
                "Não achei nenhuma mensagem elegível agora. Tente de novo mais tarde.",
                ephemeral=True,
            )
            return
        await interaction.followup.send(
            f"Rodada no ar em <#{config.channel_id}>.", ephemeral=True
        )

    @quemfalou.command(name="rank", description="Placar do jogo.")
    async def cmd_rank(self, interaction: discord.Interaction) -> None:
        assert interaction.guild_id is not None
        linhas_placar = await placar(self.db, interaction.guild_id, 10)
        if not linhas_placar:
            await interaction.response.send_message(
                "Ninguém pontuou ainda.", ephemeral=True
            )
            return

        medalhas = ("🥇", "🥈", "🥉")
        linhas = []
        for posicao, (usuario_id, pontos, acertos, palpites) in enumerate(linhas_placar):
            marca = medalhas[posicao] if posicao < len(medalhas) else f"`{posicao + 1:>2}.`"
            taxa = (acertos / palpites * 100) if palpites else 0.0
            linhas.append(
                f"{marca} <@{usuario_id}> — **{pontos}** pts · "
                f"{acertos}/{palpites} ({taxa:.0f}%)"
            )

        embed = discord.Embed(
            title="Quem falou — placar",
            description="\n".join(linhas),
            color=discord.Color.blurple(),
        )
        embed.set_footer(text="pontos · acertos/palpites (taxa de acerto)")
        await interaction.response.send_message(embed=embed)

    @quemfalou.command(name="pausar", description="Para de postar rodadas novas.")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def cmd_pausar(self, interaction: discord.Interaction) -> None:
        assert interaction.guild_id is not None
        if await get_config(self.db, interaction.guild_id) is None:
            await interaction.response.send_message(
                "O jogo ainda não foi configurado.", ephemeral=True
            )
            return
        await salvar_config(self.db, interaction.guild_id, pausado=1)
        await interaction.response.send_message(
            "Jogo pausado. A rodada em andamento, se houver, continua até o fim.",
            ephemeral=True,
        )

    @quemfalou.command(name="retomar", description="Volta a postar rodadas.")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def cmd_retomar(self, interaction: discord.Interaction) -> None:
        assert interaction.guild_id is not None
        if await get_config(self.db, interaction.guild_id) is None:
            await interaction.response.send_message(
                "O jogo ainda não foi configurado.", ephemeral=True
            )
            return
        await salvar_config(self.db, interaction.guild_id, pausado=0)
        await interaction.response.send_message("Jogo retomado.", ephemeral=True)


async def setup(bot: LeviathanBot) -> None:
    await bot.add_cog(QuemFalou(bot))
