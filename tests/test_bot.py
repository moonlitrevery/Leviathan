"""Testes do sync dos slash commands em vários servidores.

O que importa aqui é o isolamento: um servidor que recusa o sync — porque o bot
não está mais nele, ou foi convidado sem o escopo ``applications.commands`` — não
pode derrubar o ``setup_hook`` nem impedir que os outros recebam os comandos.

O bot de verdade não sobe num teste, então ``sync_guilds`` e ``guild_objects``
são emprestados para um dublê que tem só o que eles leem: ``config`` e ``tree``.
"""

from __future__ import annotations

from types import SimpleNamespace

import discord
from discord import app_commands

from leviathan.bot import LeviathanBot


class _RespostaFalsa:
    status = 403
    reason = "Forbidden"


class ArvoreFalsa:
    """Árvore de comandos que registra as chamadas e pode falhar por guild."""

    def __init__(self, falhas: dict[int, Exception] | None = None) -> None:
        self.falhas = falhas or {}
        self.copiadas: list[int] = []
        self.sincronizadas: list[int] = []

    def copy_global_to(self, *, guild) -> None:
        self.copiadas.append(guild.id)

    async def sync(self, *, guild):
        if guild.id in self.falhas:
            raise self.falhas[guild.id]
        self.sincronizadas.append(guild.id)
        # Devolve uma lista de tamanho previsível para conferir a contagem.
        return ["/ping", "/reload"]


class BotFalso:
    guild_objects = LeviathanBot.guild_objects
    sync_guilds = LeviathanBot.sync_guilds

    def __init__(self, ids: tuple[int, ...], falhas=None) -> None:
        self.config = SimpleNamespace(guild_ids=ids)
        self.tree = ArvoreFalsa(falhas)


def test_guild_objects_vira_um_objeto_por_id():
    bot = BotFalso((123, 456))

    objetos = bot.guild_objects

    assert [o.id for o in objetos] == [123, 456]
    assert all(isinstance(o, discord.Object) for o in objetos)


async def test_um_servidor_so_continua_sincronizando():
    """O .env de quem já usava um servidor só se comporta como antes."""
    bot = BotFalso((123,))

    assert list(await bot.sync_guilds()) == [123]
    assert bot.tree.copiadas == [123]


async def test_sincroniza_todos_os_servidores_da_lista():
    bot = BotFalso((123, 456, 789))

    resultado = await bot.sync_guilds()

    assert list(resultado) == [123, 456, 789]
    assert bot.tree.copiadas == [123, 456, 789], "os globais vão para cada guild"
    assert all(len(comandos) == 2 for comandos in resultado.values())


async def test_guild_que_falha_nao_impede_as_outras():
    """O caso comum: o bot não está mais em um dos servidores da lista."""
    bot = BotFalso(
        (123, 456, 789),
        falhas={456: discord.Forbidden(_RespostaFalsa(), "sem escopo")},
    )

    resultado = await bot.sync_guilds()

    assert list(resultado) == [123, 789]
    assert 456 not in resultado
    assert bot.tree.sincronizadas == [123, 789]


async def test_falha_de_http_tambem_e_isolada():
    bot = BotFalso(
        (123, 456),
        falhas={123: discord.HTTPException(_RespostaFalsa(), "500")},
    )

    resultado = await bot.sync_guilds()

    assert list(resultado) == [456]


async def test_limite_de_comandos_de_uma_guild_nao_derruba_o_resto():
    bot = BotFalso(
        (123, 456),
        falhas={123: app_commands.CommandLimitReached(guild_id=123, limit=100)},
    )

    resultado = await bot.sync_guilds()

    assert list(resultado) == [456]


async def test_todas_falhando_devolve_vazio_sem_levantar():
    """Se isso levantasse, o bot não subiria por causa de configuração de guild."""
    bot = BotFalso(
        (123, 456),
        falhas={
            123: discord.Forbidden(_RespostaFalsa(), "x"),
            456: discord.HTTPException(_RespostaFalsa(), "y"),
        },
    )

    assert await bot.sync_guilds() == {}


async def test_lista_vazia_nao_explode():
    assert await BotFalso(()).sync_guilds() == {}


async def test_uma_linha_de_log_por_guild(caplog):
    bot = BotFalso((123, 456))

    with caplog.at_level("INFO", logger="leviathan.bot"):
        await bot.sync_guilds()

    sincronizados = [
        r for r in caplog.records if "sincronizados na guild" in r.getMessage()
    ]
    assert len(sincronizados) == 2


async def test_guild_que_falha_vira_warning_com_o_id(caplog):
    bot = BotFalso((123,), falhas={123: discord.Forbidden(_RespostaFalsa(), "x")})

    with caplog.at_level("WARNING", logger="leviathan.bot"):
        await bot.sync_guilds()

    avisos = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert any("123" in aviso for aviso in avisos)
    assert any("applications.commands" in aviso for aviso in avisos), (
        "o aviso precisa dizer o que conferir"
    )
