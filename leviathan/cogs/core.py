"""Comandos básicos de operação do bot: ``/ping`` e ``/reload``."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import TypeVar

import discord
from discord import app_commands
from discord.ext import commands

from leviathan.bot import COGS_PACKAGE, LeviathanBot, discover_cogs

log = logging.getLogger(__name__)

T = TypeVar("T")


def owner_only() -> Callable[[T], T]:
    """Restringe o comando ao dono da aplicação no portal do Discord."""

    async def predicate(interaction: discord.Interaction) -> bool:
        bot = interaction.client
        assert isinstance(bot, commands.Bot)
        if await bot.is_owner(interaction.user):
            return True
        raise app_commands.CheckFailure("Apenas o dono do bot pode usar este comando.")

    return app_commands.check(predicate)


def _short_name(extension: str) -> str:
    """``leviathan.cogs.core`` -> ``core``."""
    return extension.removeprefix(f"{COGS_PACKAGE}.")


class Core(commands.Cog):
    """Diagnóstico e manutenção em runtime."""

    def __init__(self, bot: LeviathanBot) -> None:
        self.bot = bot

    @app_commands.command(name="ping", description="Mostra a latência do bot.")
    async def ping(self, interaction: discord.Interaction) -> None:
        # A latência do gateway é o heartbeat; a de API é o ida-e-volta real
        # desta interação, que é o que o usuário de fato sente.
        gateway_ms = self.bot.latency * 1000
        started = time.perf_counter()
        await interaction.response.send_message("Pong!")
        api_ms = (time.perf_counter() - started) * 1000

        await interaction.edit_original_response(
            content=f"Pong! Gateway: `{gateway_ms:.0f}ms` · API: `{api_ms:.0f}ms`"
        )

    @app_commands.command(name="reload", description="Recarrega um cog sem reiniciar o bot.")
    @app_commands.describe(cog="Nome do cog, por exemplo: core")
    @owner_only()
    async def reload(self, interaction: discord.Interaction, cog: str) -> None:
        extension = cog.strip()
        if not extension.startswith(f"{COGS_PACKAGE}."):
            extension = f"{COGS_PACKAGE}.{extension}"

        disponiveis = discover_cogs()
        if extension not in disponiveis:
            nomes = ", ".join(_short_name(e) for e in disponiveis) or "nenhum"
            await interaction.response.send_message(
                f"Cog `{cog}` não encontrado. Disponíveis: {nomes}.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            await self.bot.reload_extension(extension)
        except commands.ExtensionNotLoaded:
            await self.bot.load_extension(extension)
        except commands.ExtensionError as exc:
            log.exception("Falha ao recarregar %s", extension)
            await interaction.followup.send(
                f"Falha ao recarregar `{_short_name(extension)}`: ```{exc}```",
                ephemeral=True,
            )
            return

        # Assinaturas de comando podem ter mudado, então re-sincroniza a guild.
        await self.bot.sync_dev_guild()
        log.info("Cog %s recarregado por %s", extension, interaction.user)
        await interaction.followup.send(
            f"Cog `{_short_name(extension)}` recarregado e comandos sincronizados.",
            ephemeral=True,
        )

    @reload.autocomplete("cog")
    async def reload_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        termo = current.strip().lower()
        return [
            app_commands.Choice(name=nome, value=nome)
            for extension in discover_cogs()
            if termo in (nome := _short_name(extension))
        ][:25]


async def setup(bot: LeviathanBot) -> None:
    await bot.add_cog(Core(bot))
