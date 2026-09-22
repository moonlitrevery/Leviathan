"""Gatilhos de texto e contadores por reação.

O módulo é dividido em duas camadas de propósito: funções livres no topo, que só
falam com o :class:`~leviathan.db.Database` e com strings (e por isso são testáveis
sem subir nada do Discord), e o cog no fim, que cuida de eventos, comandos e embeds.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass
from functools import lru_cache

import discord
from discord import app_commands
from discord.ext import commands

from leviathan.bot import LeviathanBot
from leviathan.db import Database
from leviathan.emoji import emoji_key, normalize_unicode_emoji, parse_emoji

log = logging.getLogger(__name__)

#: Gatilhos criados automaticamente numa guild que ainda não tem nenhum.
SEED_TRIGGERS: tuple[tuple[str, str, bool], ...] = (
    (
        "papoi",
        "https://images-ext-1.discordapp.net/external/"
        "_XGgaht0Vl3PgL3NoYcKew_088_Ww1VVyyDBKVRJjss/"
        "%3Furl%3Dhttps%3A%2F%2Fvideo.twimg.com%2Ftweet_video%2FHRsnGnAaoAA3a5S.mp4"
        "/https/gifconvert.vxtwitter.com/convert.avif?animated=true&format=webp",
        False,
    ),
)

#: Janela mínima entre duas respostas do mesmo gatilho no mesmo canal.
TRIGGER_COOLDOWN_SECONDS = 30.0

#: Idade a partir da qual uma entrada de cooldown já não serve para nada.
COOLDOWN_TTL_SECONDS = 300.0

#: Espera antes de publicar o placar, para agrupar rajadas de reações.
SCOREBOARD_DEBOUNCE_SECONDS = 3.0

#: Quantas posições aparecem no placar fixo e no ``/contador rank``.
TOP_N_PLACAR = 3
RANK_LIMIT = 25

MEDALHAS = ("🥇", "🥈", "🥉")


# ---------------------------------------------------------------------------
# Gatilhos de texto
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Trigger:
    """Uma palavra-gatilho e a URL que o bot responde quando ela aparece."""

    id: int
    guild_id: int
    word: str
    url: str
    substring: bool


@lru_cache(maxsize=512)
def compile_trigger(word: str, substring: bool) -> re.Pattern[str]:
    """Compila o padrão de um gatilho.

    No modo padrão o casamento é por palavra inteira. Usamos ``(?<!\\w)``/``(?!\\w)``
    em vez de ``\\b`` porque são equivalentes para palavras normais e continuam
    funcionando quando o gatilho começa ou termina com pontuação (``\\b`` nunca
    casaria nesse caso). O modo ``substring`` casa em qualquer posição.
    """
    escapada = re.escape(word)
    padrao = escapada if substring else rf"(?<!\w){escapada}(?!\w)"
    return re.compile(padrao, re.IGNORECASE)


def find_match(triggers: list[Trigger], content: str) -> Trigger | None:
    """Devolve o primeiro gatilho que casa com a mensagem, ou ``None``.

    Só o primeiro: uma mensagem que cite vários gatilhos rende uma resposta, não uma
    rajada. Os gatilhos chegam ordenados por palavra, então a escolha é estável.
    """
    for trigger in triggers:
        if compile_trigger(trigger.word, trigger.substring).search(content):
            return trigger
    return None


async def list_triggers(db: Database, guild_id: int) -> list[Trigger]:
    """Gatilhos cadastrados na guild, em ordem alfabética."""
    rows = await db.fetchall(
        "SELECT id, guild_id, word, url, substring FROM triggers"
        " WHERE guild_id = ? ORDER BY word",
        (guild_id,),
    )
    return [
        Trigger(
            id=row["id"],
            guild_id=row["guild_id"],
            word=row["word"],
            url=row["url"],
            substring=bool(row["substring"]),
        )
        for row in rows
    ]


async def count_triggers(db: Database, guild_id: int) -> int:
    row = await db.fetchone(
        "SELECT COUNT(*) AS total FROM triggers WHERE guild_id = ?", (guild_id,)
    )
    return int(row["total"]) if row else 0


async def upsert_trigger(
    db: Database,
    guild_id: int,
    word: str,
    url: str,
    substring: bool,
) -> str:
    """Cadastra ou atualiza um gatilho. Devolve ``"criado"`` ou ``"atualizado"``."""
    existente = await db.fetchone(
        "SELECT id FROM triggers WHERE guild_id = ? AND word = ?",
        (guild_id, word),
    )
    if existente is not None:
        await db.execute(
            "UPDATE triggers SET url = ?, substring = ? WHERE id = ?",
            (url, int(substring), existente["id"]),
        )
        return "atualizado"

    await db.execute(
        "INSERT INTO triggers (guild_id, word, url, substring) VALUES (?, ?, ?, ?)",
        (guild_id, word, url, int(substring)),
    )
    return "criado"


async def remove_trigger(db: Database, guild_id: int, word: str) -> int:
    """Remove o gatilho da guild. Devolve quantas linhas saíram."""
    return await db.execute(
        "DELETE FROM triggers WHERE guild_id = ? AND word = ?",
        (guild_id, word),
    )


async def seed_guild_triggers(db: Database, guild_id: int) -> list[str]:
    """Aplica :data:`SEED_TRIGGERS` se a guild ainda não tiver gatilho nenhum.

    A condição é "nenhum gatilho", e não "este gatilho": quem apagar o ``papoi`` e
    tiver outros cadastrados não o vê voltar no próximo boot.
    """
    if await count_triggers(db, guild_id):
        return []

    for word, url, substring in SEED_TRIGGERS:
        await upsert_trigger(db, guild_id, word, url, substring)
    return [word for word, _, _ in SEED_TRIGGERS]


# ---------------------------------------------------------------------------
# Contadores por reação
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Counter:
    """Um contador ligado a um emoji, com seu placar fixo em algum canal."""

    id: int
    guild_id: int
    name: str
    emoji_key: str
    emoji_display: str
    scoreboard_channel_id: int
    scoreboard_message_id: int | None


def _counter_from_row(row) -> Counter:
    return Counter(
        id=row["id"],
        guild_id=row["guild_id"],
        name=row["name"],
        emoji_key=row["emoji_key"],
        emoji_display=row["emoji_display"],
        scoreboard_channel_id=row["scoreboard_channel_id"],
        scoreboard_message_id=row["scoreboard_message_id"],
    )


_COUNTER_COLUMNS = (
    "id, guild_id, name, emoji_key, emoji_display,"
    " scoreboard_channel_id, scoreboard_message_id"
)


async def get_counter(db: Database, counter_id: int) -> Counter | None:
    row = await db.fetchone(
        f"SELECT {_COUNTER_COLUMNS} FROM counters WHERE id = ?", (counter_id,)
    )
    return _counter_from_row(row) if row else None


async def get_counter_by_emoji(db: Database, guild_id: int, key: str) -> Counter | None:
    row = await db.fetchone(
        f"SELECT {_COUNTER_COLUMNS} FROM counters WHERE guild_id = ? AND emoji_key = ?",
        (guild_id, normalize_unicode_emoji(key)),
    )
    return _counter_from_row(row) if row else None


async def get_counter_by_name(db: Database, guild_id: int, name: str) -> Counter | None:
    row = await db.fetchone(
        f"SELECT {_COUNTER_COLUMNS} FROM counters"
        " WHERE guild_id = ? AND name = ? COLLATE NOCASE",
        (guild_id, name),
    )
    return _counter_from_row(row) if row else None


async def list_counters(db: Database, guild_id: int) -> list[Counter]:
    rows = await db.fetchall(
        f"SELECT {_COUNTER_COLUMNS} FROM counters WHERE guild_id = ? ORDER BY name",
        (guild_id,),
    )
    return [_counter_from_row(row) for row in rows]


async def create_counter(
    db: Database,
    guild_id: int,
    name: str,
    emoji: discord.PartialEmoji,
    channel_id: int,
) -> Counter:
    await db.execute(
        "INSERT INTO counters"
        " (guild_id, name, emoji_key, emoji_display, scoreboard_channel_id)"
        " VALUES (?, ?, ?, ?, ?)",
        (guild_id, name, emoji_key(emoji), str(emoji), channel_id),
    )
    criado = await get_counter_by_name(db, guild_id, name)
    assert criado is not None
    return criado


async def delete_counter(db: Database, counter_id: int) -> int:
    """Apaga o contador. Os hits somem junto, via ``ON DELETE CASCADE``."""
    return await db.execute("DELETE FROM counters WHERE id = ?", (counter_id,))


async def set_scoreboard_message(db: Database, counter_id: int, message_id: int) -> None:
    await db.execute(
        "UPDATE counters SET scoreboard_message_id = ? WHERE id = ?",
        (message_id, counter_id),
    )


async def record_hit(
    db: Database,
    counter_id: int,
    message_id: int,
    reactor_id: int,
    target_id: int,
) -> bool:
    """Registra uma reação. Devolve ``True`` só se ela ainda não tinha sido contada.

    A deduplicação é a chave primária ``(counter_id, message_id, reactor_id)``:
    como o emoji é único por contador, essa trinca é exatamente
    ``(message_id, emoji, user_id)``. Um ``INSERT OR IGNORE`` que não insere nada
    devolve ``rowcount == 0``, e é isso que transforma a repetição em no-op.
    """
    afetadas = await db.execute(
        "INSERT OR IGNORE INTO counter_hits (counter_id, message_id, reactor_id, target_id)"
        " VALUES (?, ?, ?, ?)",
        (counter_id, message_id, reactor_id, target_id),
    )
    return afetadas > 0


async def counter_total(db: Database, counter_id: int) -> int:
    row = await db.fetchone(
        "SELECT COUNT(*) AS total FROM counter_hits WHERE counter_id = ?", (counter_id,)
    )
    return int(row["total"]) if row else 0


async def counter_ranking(
    db: Database, counter_id: int, limit: int
) -> list[tuple[int, int]]:
    """``[(user_id, quantidade)]`` de quem mais recebeu a reação, do maior ao menor."""
    rows = await db.fetchall(
        "SELECT target_id, COUNT(*) AS total FROM counter_hits"
        " WHERE counter_id = ?"
        " GROUP BY target_id"
        " ORDER BY total DESC, target_id ASC"
        " LIMIT ?",
        (counter_id, limit),
    )
    return [(int(row["target_id"]), int(row["total"])) for row in rows]


async def counter_distinct_targets(db: Database, counter_id: int) -> int:
    row = await db.fetchone(
        "SELECT COUNT(DISTINCT target_id) AS total FROM counter_hits WHERE counter_id = ?",
        (counter_id,),
    )
    return int(row["total"]) if row else 0


# ---------------------------------------------------------------------------
# Cog
# ---------------------------------------------------------------------------


class Piadas(commands.Cog):
    """Gatilhos de texto e contadores por reação."""

    gatilho = app_commands.Group(
        name="gatilho",
        description="Palavras que fazem o bot responder com uma URL.",
        guild_only=True,
    )
    contador = app_commands.Group(
        name="contador",
        description="Contadores alimentados por reações.",
        guild_only=True,
    )

    def __init__(self, bot: LeviathanBot) -> None:
        self.bot = bot
        self.db = bot.db
        # Cache dos gatilhos por guild, invalidado a cada add/remove: sem ele
        # cada mensagem do servidor viraria uma consulta ao banco.
        self._triggers: dict[int, list[Trigger]] = {}
        # (channel_id, trigger_id) -> instante da última resposta. Podado por idade.
        self._cooldowns: dict[tuple[int, int], float] = {}
        self._last_cooldown_prune = 0.0
        # Um lock por contador para que reações simultâneas não briguem pelo placar.
        # Dict simples (não defaultdict) para não criar lock por id inexistente.
        self._scoreboard_locks: dict[int, asyncio.Lock] = {}
        # Publicações de placar agendadas, uma por contador.
        self._pending_scoreboards: dict[int, asyncio.Task[None]] = {}
        self._seeded_guilds: set[int] = set()

    # -- ciclo de vida -----------------------------------------------------

    async def cog_load(self) -> None:
        # Num /reload o bot já está conectado e o on_ready não vai repetir,
        # então o seed também é tentado aqui.
        if self.bot.is_ready():
            await self._seed_all_guilds()

    def cog_unload(self) -> None:
        """Cancela os placares agendados, para o /reload não deixar task órfã."""
        for task in self._pending_scoreboards.values():
            task.cancel()
        self._pending_scoreboards.clear()
        self._scoreboard_locks.clear()

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        await self._seed_all_guilds()

    async def _seed_all_guilds(self) -> None:
        for guild in self.bot.guilds:
            if guild.id in self._seeded_guilds:
                continue
            self._seeded_guilds.add(guild.id)
            criados = await seed_guild_triggers(self.db, guild.id)
            if criados:
                self._invalidate_triggers(guild.id)
                log.info(
                    "Gatilhos padrão criados na guild %s (%d): %s",
                    guild.name,
                    guild.id,
                    ", ".join(criados),
                )

    # -- gatilhos: eventos ------------------------------------------------

    async def _get_triggers(self, guild_id: int) -> list[Trigger]:
        cached = self._triggers.get(guild_id)
        if cached is None:
            cached = await list_triggers(self.db, guild_id)
            self._triggers[guild_id] = cached
        return cached

    def _invalidate_triggers(self, guild_id: int) -> None:
        self._triggers.pop(guild_id, None)

    def _prune_cooldowns(self, agora: float) -> None:
        """Descarta cooldowns já vencidos, no máximo uma vez por TTL."""
        if agora - self._last_cooldown_prune < COOLDOWN_TTL_SECONDS:
            return
        self._last_cooldown_prune = agora
        limite = agora - COOLDOWN_TTL_SECONDS
        self._cooldowns = {
            chave: quando for chave, quando in self._cooldowns.items() if quando > limite
        }

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or message.guild is None or not message.content:
            return

        triggers = await self._get_triggers(message.guild.id)
        if not triggers:
            return

        acertou = find_match(triggers, message.content)
        if acertou is None:
            return

        agora = time.monotonic()
        self._prune_cooldowns(agora)

        chave = (message.channel.id, acertou.id)
        if agora - self._cooldowns.get(chave, 0.0) < TRIGGER_COOLDOWN_SECONDS:
            return
        self._cooldowns[chave] = agora

        try:
            await message.reply(acertou.url, mention_author=False)
        except discord.HTTPException:
            log.exception("Falha ao responder o gatilho %r", acertou.word)

    # -- gatilhos: comandos -----------------------------------------------

    @gatilho.command(name="add", description="Cadastra ou atualiza uma palavra-gatilho.")
    @app_commands.describe(
        palavra="Palavra que dispara a resposta",
        url="URL que o bot responde",
        substring="Casar também no meio de outras palavras (padrão: não)",
    )
    @app_commands.checks.has_permissions(manage_guild=True)
    async def gatilho_add(
        self,
        interaction: discord.Interaction,
        palavra: str,
        url: str,
        substring: bool = False,
    ) -> None:
        word = palavra.strip().lower()
        url = url.strip()

        if not word:
            await interaction.response.send_message(
                "A palavra não pode ser vazia.", ephemeral=True
            )
            return
        if not url.startswith(("http://", "https://")):
            await interaction.response.send_message(
                "A URL precisa começar com `http://` ou `https://`.", ephemeral=True
            )
            return

        assert interaction.guild_id is not None
        acao = await upsert_trigger(self.db, interaction.guild_id, word, url, substring)
        self._invalidate_triggers(interaction.guild_id)

        modo = "substring" if substring else "palavra inteira"
        await interaction.response.send_message(
            f"Gatilho `{word}` {acao} ({modo}) → {url}", ephemeral=True
        )

    @gatilho.command(name="remove", description="Remove uma palavra-gatilho.")
    @app_commands.describe(palavra="Palavra cadastrada")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def gatilho_remove(self, interaction: discord.Interaction, palavra: str) -> None:
        word = palavra.strip().lower()
        assert interaction.guild_id is not None

        removidos = await remove_trigger(self.db, interaction.guild_id, word)
        self._invalidate_triggers(interaction.guild_id)

        if removidos:
            await interaction.response.send_message(
                f"Gatilho `{word}` removido.", ephemeral=True
            )
        else:
            await interaction.response.send_message(
                f"Não achei nenhum gatilho `{word}`.", ephemeral=True
            )

    @gatilho.command(name="list", description="Lista os gatilhos deste servidor.")
    async def gatilho_list(self, interaction: discord.Interaction) -> None:
        assert interaction.guild_id is not None
        triggers = await self._get_triggers(interaction.guild_id)

        if not triggers:
            await interaction.response.send_message(
                "Nenhum gatilho cadastrado. Use `/gatilho add`.", ephemeral=True
            )
            return

        linhas = []
        for trigger in triggers:
            sufixo = " _(substring)_" if trigger.substring else ""
            linhas.append(f"**{trigger.word}**{sufixo}\n{trigger.url}")

        embed = discord.Embed(
            title=f"Gatilhos ({len(triggers)})",
            description="\n\n".join(linhas)[:4000],
            color=discord.Color.blurple(),
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @gatilho_remove.autocomplete("palavra")
    async def gatilho_palavra_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        if interaction.guild_id is None:
            return []
        termo = current.strip().lower()
        triggers = await self._get_triggers(interaction.guild_id)
        return [
            app_commands.Choice(name=t.word, value=t.word)
            for t in triggers
            if termo in t.word
        ][:25]

    # -- contadores: eventos ----------------------------------------------

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent) -> None:
        # raw_reaction_add (e não reaction_add) para pegar também mensagens antigas,
        # que não estão no cache do bot.
        if payload.guild_id is None:
            return
        if payload.member is not None and payload.member.bot:
            return
        if self.bot.user is not None and payload.user_id == self.bot.user.id:
            return

        counter = await get_counter_by_emoji(self.db, payload.guild_id, emoji_key(payload.emoji))
        if counter is None:
            return

        # Só agora vale buscar a mensagem: precisamos do autor para saber quem recebeu.
        message = await self._fetch_message(payload.channel_id, payload.message_id)
        if message is None or message.author.bot:
            return

        contou = await record_hit(
            self.db,
            counter.id,
            payload.message_id,
            payload.user_id,
            message.author.id,
        )
        if not contou:
            log.debug(
                "Reação repetida ignorada (contador=%s, mensagem=%s, usuário=%s)",
                counter.name,
                payload.message_id,
                payload.user_id,
            )
            return

        self.schedule_scoreboard(counter.id)

    async def _fetch_message(self, channel_id: int, message_id: int) -> discord.Message | None:
        channel = self.bot.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(channel_id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                return None
        if not isinstance(channel, discord.abc.Messageable):
            return None
        try:
            return await channel.fetch_message(message_id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return None

    # -- contadores: placar -----------------------------------------------

    def schedule_scoreboard(self, counter_id: int) -> None:
        """Marca o placar como sujo e agenda a publicação.

        Cada reação nova cancela a publicação pendente e reagenda: numa rajada de
        reações o bot faz uma edição só, em vez de uma por pessoa, o que evita
        bater no rate limit do Discord.
        """
        pendente = self._pending_scoreboards.get(counter_id)
        if pendente is not None and not pendente.done():
            pendente.cancel()

        self._pending_scoreboards[counter_id] = asyncio.create_task(
            self._debounced_publish(counter_id),
            name=f"placar-{counter_id}",
        )

    async def _debounced_publish(self, counter_id: int) -> None:
        try:
            await asyncio.sleep(SCOREBOARD_DEBOUNCE_SECONDS)
            await self.publish_scoreboard(counter_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Falha ao publicar o placar do contador %d", counter_id)
        finally:
            # Só limpa se ninguém reagendou por cima desta task.
            if self._pending_scoreboards.get(counter_id) is asyncio.current_task():
                self._pending_scoreboards.pop(counter_id, None)

    def _cancel_scoreboard(self, counter_id: int) -> None:
        pendente = self._pending_scoreboards.pop(counter_id, None)
        if pendente is not None and not pendente.done():
            pendente.cancel()

    async def build_scoreboard_embed(self, counter: Counter) -> discord.Embed:
        total = await counter_total(self.db, counter.id)
        embed = discord.Embed(
            title=f"Quantidade de vezes que {counter.name}: {total}",
            description=f"Reaja com {counter.emoji_display} em qualquer mensagem para contar.",
            color=discord.Color.blurple(),
        )

        ranking = await counter_ranking(self.db, counter.id, TOP_N_PLACAR)
        if ranking:
            linhas = [
                f"{MEDALHAS[i]} <@{user_id}> — **{quantidade}**"
                for i, (user_id, quantidade) in enumerate(ranking)
            ]
            embed.add_field(name="Top 3", value="\n".join(linhas), inline=False)
        else:
            embed.add_field(name="Top 3", value="Ainda ninguém.", inline=False)

        return embed

    async def publish_scoreboard(self, counter_id: int) -> None:
        """Edita a mensagem de placar do contador, recriando-a se tiver sumido."""
        # Checagem antes do lock: contador que não existe não ganha lock no dict.
        if await get_counter(self.db, counter_id) is None:
            self._scoreboard_locks.pop(counter_id, None)
            return

        lock = self._scoreboard_locks.setdefault(counter_id, asyncio.Lock())
        async with lock:
            # Releitura dentro do lock: o scoreboard_message_id pode ter mudado
            # enquanto esperávamos.
            counter = await get_counter(self.db, counter_id)
            if counter is None:
                return

            channel = self.bot.get_channel(counter.scoreboard_channel_id)
            if channel is None:
                try:
                    channel = await self.bot.fetch_channel(counter.scoreboard_channel_id)
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    log.warning(
                        "Canal de placar %d do contador %r está inacessível",
                        counter.scoreboard_channel_id,
                        counter.name,
                    )
                    return
            if not isinstance(channel, discord.abc.Messageable):
                return

            embed = await self.build_scoreboard_embed(counter)

            if counter.scoreboard_message_id is not None:
                try:
                    mensagem = await channel.fetch_message(counter.scoreboard_message_id)
                except discord.NotFound:
                    log.info("Placar de %r foi apagado, recriando", counter.name)
                except (discord.Forbidden, discord.HTTPException):
                    log.exception("Falha ao ler o placar de %r", counter.name)
                    return
                else:
                    try:
                        await mensagem.edit(embed=embed)
                        return
                    except discord.HTTPException:
                        log.exception("Falha ao editar o placar de %r", counter.name)
                        return

            try:
                nova = await channel.send(embed=embed)
            except (discord.Forbidden, discord.HTTPException):
                log.exception("Falha ao publicar o placar de %r", counter.name)
                return
            await set_scoreboard_message(self.db, counter.id, nova.id)

    # -- contadores: comandos ---------------------------------------------

    @contador.command(name="criar", description="Cria um contador ligado a um emoji.")
    @app_commands.describe(
        emoji="Emoji que alimenta o contador (unicode ou custom do servidor)",
        nome="Como o contador é descrito, ex: o João caiu",
        canal_placar="Canal onde a mensagem de placar fica fixa",
    )
    @app_commands.checks.has_permissions(manage_guild=True)
    async def contador_criar(
        self,
        interaction: discord.Interaction,
        emoji: str,
        nome: str,
        canal_placar: discord.TextChannel,
    ) -> None:
        assert interaction.guild_id is not None
        nome = " ".join(nome.split())

        parsed = parse_emoji(emoji)
        if parsed is None:
            await interaction.response.send_message(
                f"`{emoji}` não parece um emoji. Use um emoji unicode (🪱) "
                "ou um emoji custom deste servidor.",
                ephemeral=True,
            )
            return
        if not nome:
            await interaction.response.send_message(
                "O nome do contador não pode ser vazio.", ephemeral=True
            )
            return

        if await get_counter_by_name(self.db, interaction.guild_id, nome) is not None:
            await interaction.response.send_message(
                f"Já existe um contador chamado **{nome}**.", ephemeral=True
            )
            return
        existente = await get_counter_by_emoji(
            self.db, interaction.guild_id, emoji_key(parsed)
        )
        if existente is not None:
            await interaction.response.send_message(
                f"O emoji {parsed} já alimenta o contador **{existente.name}**.",
                ephemeral=True,
            )
            return

        permissoes = canal_placar.permissions_for(canal_placar.guild.me)
        if not (permissoes.send_messages and permissoes.embed_links):
            await interaction.response.send_message(
                f"Não consigo publicar o placar em {canal_placar.mention}: "
                "preciso de 'Enviar mensagens' e 'Inserir links'.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        counter = await create_counter(
            self.db, interaction.guild_id, nome, parsed, canal_placar.id
        )
        # Publicação direta: aqui o placar é a confirmação de que deu certo.
        await self.publish_scoreboard(counter.id)

        await interaction.followup.send(
            f"Contador **{nome}** criado com {parsed}. "
            f"O placar fica em {canal_placar.mention}.",
            ephemeral=True,
        )

    @contador.command(name="list", description="Lista os contadores do servidor.")
    async def contador_list(self, interaction: discord.Interaction) -> None:
        assert interaction.guild_id is not None
        counters = await list_counters(self.db, interaction.guild_id)

        if not counters:
            await interaction.response.send_message(
                "Nenhum contador criado. Use `/contador criar`.", ephemeral=True
            )
            return

        linhas = []
        for counter in counters:
            total = await counter_total(self.db, counter.id)
            linhas.append(
                f"{counter.emoji_display} **{counter.name}** — {total} "
                f"(placar em <#{counter.scoreboard_channel_id}>)"
            )

        embed = discord.Embed(
            title=f"Contadores ({len(counters)})",
            description="\n".join(linhas)[:4000],
            color=discord.Color.blurple(),
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @contador.command(name="remover", description="Remove um contador e seu histórico.")
    @app_commands.describe(nome="Nome do contador")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def contador_remover(self, interaction: discord.Interaction, nome: str) -> None:
        assert interaction.guild_id is not None
        counter = await get_counter_by_name(self.db, interaction.guild_id, nome.strip())
        if counter is None:
            await interaction.response.send_message(
                f"Não achei o contador **{nome}**.", ephemeral=True
            )
            return

        total = await counter_total(self.db, counter.id)
        await delete_counter(self.db, counter.id)
        self._cancel_scoreboard(counter.id)
        self._scoreboard_locks.pop(counter.id, None)
        await interaction.response.send_message(
            f"Contador **{counter.name}** removido ({total} registros apagados). "
            "A mensagem de placar continua no canal, apague se quiser.",
            ephemeral=True,
        )

    @contador.command(name="rank", description="Ranking completo de um contador.")
    @app_commands.describe(nome="Nome do contador")
    async def contador_rank(self, interaction: discord.Interaction, nome: str) -> None:
        assert interaction.guild_id is not None
        counter = await get_counter_by_name(self.db, interaction.guild_id, nome.strip())
        if counter is None:
            await interaction.response.send_message(
                f"Não achei o contador **{nome}**.", ephemeral=True
            )
            return

        total = await counter_total(self.db, counter.id)
        ranking = await counter_ranking(self.db, counter.id, RANK_LIMIT)
        distintos = await counter_distinct_targets(self.db, counter.id)

        embed = discord.Embed(
            title=f"Quantidade de vezes que {counter.name}: {total}",
            color=discord.Color.blurple(),
        )
        if ranking:
            linhas = [
                f"{MEDALHAS[i] if i < len(MEDALHAS) else f'`{i + 1:>2}.`'}"
                f" <@{user_id}> — **{quantidade}**"
                for i, (user_id, quantidade) in enumerate(ranking)
            ]
            embed.description = "\n".join(linhas)
            if distintos > len(ranking):
                embed.set_footer(
                    text=f"Mostrando {len(ranking)} de {distintos} pessoas."
                )
        else:
            embed.description = "Ninguém recebeu essa reação ainda."

        await interaction.response.send_message(embed=embed)

    @contador_remover.autocomplete("nome")
    @contador_rank.autocomplete("nome")
    async def contador_nome_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        if interaction.guild_id is None:
            return []
        termo = current.strip().lower()
        counters = await list_counters(self.db, interaction.guild_id)
        return [
            app_commands.Choice(name=c.name, value=c.name)
            for c in counters
            if termo in c.name.lower()
        ][:25]


async def setup(bot: LeviathanBot) -> None:
    await bot.add_cog(Piadas(bot))
