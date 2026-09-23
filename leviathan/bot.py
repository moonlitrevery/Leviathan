"""Subclasse de ``commands.Bot`` com carga automática de cogs e sync por guild."""

from __future__ import annotations

import logging
import traceback
from pathlib import Path

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands

from leviathan import __version__
from leviathan.config import Config
from leviathan.db import Database, init_db

log = logging.getLogger(__name__)

#: Pasta varrida em busca de cogs.
COGS_DIR = Path(__file__).resolve().parent / "cogs"

#: Prefixo de import dos cogs (``leviathan.cogs.core``, por exemplo).
COGS_PACKAGE = "leviathan.cogs"

#: Teto das chamadas HTTP dos cogs. Uma API lenta não pode segurar o bot.
HTTP_TIMEOUT = aiohttp.ClientTimeout(total=10)

USER_AGENT = f"LeviathanBot/{__version__} (+https://github.com/moonlitrevery/Leviathan)"


class CogUnavailable(Exception):
    """Cog que não pode operar neste ambiente — falta de config ou de asset.

    Não é defeito: quem levanta isso no ``setup`` do cog vira um aviso de uma
    linha no log, sem traceback, e os outros cogs seguem carregando.
    """


def discover_cogs() -> list[str]:
    """Lista os caminhos de extensão de todo ``.py`` em ``leviathan/cogs``.

    Arquivos começando com ``_`` (como ``__init__.py``) são ignorados, o que dá
    uma saída simples para módulos auxiliares que não são cogs.
    """
    if not COGS_DIR.is_dir():
        return []
    return [
        f"{COGS_PACKAGE}.{path.stem}"
        for path in sorted(COGS_DIR.glob("*.py"))
        if not path.stem.startswith("_")
    ]


class LeviathanBot(commands.Bot):
    """O bot em si: mantém a config, o banco e o ciclo de vida das extensões."""

    def __init__(self, config: Config) -> None:
        intents = discord.Intents.default()
        intents.message_content = True
        intents.members = True

        super().__init__(
            command_prefix=commands.when_mentioned,
            intents=intents,
            help_command=None,
            allowed_mentions=discord.AllowedMentions.none(),
        )

        self.config = config
        self.db = Database(config.database_path)
        # Sessão HTTP única, compartilhada por todos os cogs que falam com APIs.
        # Abrir uma por cog desperdiçaria pool de conexões e complicaria o shutdown.
        self.http_session: aiohttp.ClientSession | None = None
        # Handler global de erro de app command (substitui o padrão da tree).
        self.tree.on_error = self.on_app_command_error

    @property
    def guild_objects(self) -> tuple[discord.Object, ...]:
        """Os servidores do ``GUILD_ID``, prontos para a árvore de comandos."""
        return tuple(discord.Object(id=guild_id) for guild_id in self.config.guild_ids)

    # ------------------------------------------------------------------
    # Ciclo de vida
    # ------------------------------------------------------------------

    async def setup_hook(self) -> None:
        """Roda uma vez antes do login: HTTP, banco, cogs e sync dos comandos."""
        self.http_session = aiohttp.ClientSession(
            timeout=HTTP_TIMEOUT,
            headers={"User-Agent": USER_AGENT},
        )
        await self.db.connect()
        await init_db(self.db)

        await self.load_all_cogs()
        await self.sync_guilds()

    async def load_all_cogs(self) -> list[str]:
        """Carrega todos os cogs da pasta ``cogs``; devolve os carregados com sucesso."""
        loaded: list[str] = []
        for extension in discover_cogs():
            try:
                await self.load_extension(extension)
            except commands.ExtensionFailed as exc:
                # CogUnavailable é situação prevista (falta chave de API, por
                # exemplo): merece um aviso legível, não um traceback.
                if isinstance(exc.original, CogUnavailable):
                    log.warning("Cog %s não carregado: %s", extension, exc.original)
                else:
                    log.exception("Falha ao carregar o cog %s", extension)
            except commands.ExtensionError:
                log.exception("Falha ao carregar o cog %s", extension)
            else:
                loaded.append(extension)
                log.info("Cog carregado: %s", extension)

        if not loaded:
            log.warning("Nenhum cog carregado de %s", COGS_DIR)
        return loaded

    async def sync_guilds(self) -> dict[int, list[app_commands.AppCommand]]:
        """Copia os comandos globais para cada servidor do ``GUILD_ID`` e sincroniza.

        Sync por guild é instantâneo, enquanto o global leva até uma hora para
        propagar — por isso os comandos são registrados servidor a servidor, e
        não globalmente.

        Uma guild que falha não derruba as outras nem o ``setup_hook``: o caso
        comum é o bot não estar (mais) naquele servidor, ou ter sido convidado
        sem o escopo ``applications.commands``. Isso é problema de configuração
        de um servidor, não motivo para o bot não subir.

        Devolve os comandos sincronizados por guild; as que falharam ficam de
        fora do dicionário.
        """
        resultados: dict[int, list[app_commands.AppCommand]] = {}
        for guild in self.guild_objects:
            try:
                self.tree.copy_global_to(guild=guild)
                synced = await self.tree.sync(guild=guild)
            except discord.Forbidden:
                log.warning(
                    "Sem permissão para sincronizar comandos na guild %d — o bot"
                    " precisa ter sido convidado com o escopo applications.commands",
                    guild.id,
                )
            except app_commands.CommandLimitReached as exc:
                log.warning("Limite de comandos atingido na guild %d: %s", guild.id, exc)
            except discord.HTTPException as exc:
                log.warning(
                    "Falha ao sincronizar os comandos na guild %d: %s", guild.id, exc
                )
            else:
                resultados[guild.id] = synced
                log.info("%d comandos sincronizados na guild %d", len(synced), guild.id)

        if not resultados:
            log.warning(
                "Nenhuma guild sincronizada — os slash commands não vão aparecer."
                " Confira o GUILD_ID e se o bot está nesses servidores."
            )
        return resultados

    async def close(self) -> None:
        """Encerra a conexão com o Discord, a sessão HTTP e o pool do banco."""
        try:
            await super().close()
        finally:
            if self.http_session is not None:
                await self.http_session.close()
                self.http_session = None
            await self.db.close()

    async def on_ready(self) -> None:
        if self.user is not None:
            log.info("Conectado como %s (id=%d)", self.user, self.user.id)

    # ------------------------------------------------------------------
    # Tratamento de erro
    # ------------------------------------------------------------------

    async def on_app_command_error(
        self,
        interaction: discord.Interaction,
        error: app_commands.AppCommandError,
    ) -> None:
        """Responde ao usuário de forma amigável e loga o traceback completo."""
        message = _friendly_message(error)

        command = interaction.command.qualified_name if interaction.command else "desconhecido"
        log.error(
            "Erro no app command /%s (usuário=%s, guild=%s):\n%s",
            command,
            interaction.user,
            interaction.guild_id,
            "".join(traceback.format_exception(type(error), error, error.__traceback__)).rstrip(),
        )

        await _respond_quietly(interaction, message)


def _friendly_message(error: app_commands.AppCommandError) -> str:
    """Traduz o erro para algo que faça sentido para quem usou o comando."""
    if isinstance(error, app_commands.CommandOnCooldown):
        return f"Calma aí — tente de novo em {error.retry_after:.1f}s."
    if isinstance(error, app_commands.MissingPermissions):
        faltando = ", ".join(error.missing_permissions)
        return f"Você não tem permissão para isso (falta: {faltando})."
    if isinstance(error, app_commands.BotMissingPermissions):
        faltando = ", ".join(error.missing_permissions)
        return f"Eu não tenho permissão para isso (falta: {faltando})."
    if isinstance(error, app_commands.CheckFailure):
        # Checks personalizados levantam CheckFailure com uma mensagem própria.
        return str(error) or "Você não pode usar este comando."
    if isinstance(error, app_commands.CommandNotFound):
        return "Esse comando não existe mais. Tente sincronizar os comandos de novo."
    if isinstance(error, app_commands.TransformerError):
        return "Não consegui entender um dos argumentos enviados."
    return "Algo deu errado ao executar esse comando. O erro foi registrado nos logs."


async def _respond_quietly(interaction: discord.Interaction, message: str) -> None:
    """Envia a mensagem de erro em ephemeral, mesmo se a interação já respondeu."""
    try:
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)
    except discord.HTTPException:
        log.exception("Não foi possível avisar o usuário sobre o erro")
