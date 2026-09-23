"""Log de vida: uma linha por hora dizendo que o bot continua de pé.

Num servidor sem ninguém olhando, um bot que caiu e um bot num canal parado são
indistinguíveis: os dois ficam calados. Esta linha por hora resolve isso — se ela
parou de aparecer no ``journalctl``/``docker logs``, alguma coisa aconteceu.

O que ela responde: o bot está conectado, quantas assinaturas de alerta existem e
se os laços de fundo continuam girando. Os laços são descobertos sozinhos em
todos os cogs carregados, então um cog novo com ``tasks.loop`` entra no relatório
sem ninguém precisar lembrar de mexer aqui.
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass
from datetime import timedelta

import discord
from discord.ext import commands, tasks

from leviathan.bot import LeviathanBot
from leviathan.db import Database

log = logging.getLogger(__name__)

INTERVALO_HORAS = 1


# ---------------------------------------------------------------------------
# Lógica pura
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class EstadoLaco:
    """Situação de um ``tasks.loop`` de algum cog."""

    nome: str
    rodando: bool
    falhou: bool

    @property
    def ok(self) -> bool:
        return self.rodando and not self.falhou

    @property
    def descricao(self) -> str:
        if self.falhou:
            return f"{self.nome}: PAROU COM ERRO"
        if not self.rodando:
            return f"{self.nome}: parado"
        return f"{self.nome}: ok"


def formatar_duracao(duracao: timedelta) -> str:
    """``3d 4h``, ``5h12m``, ``47min``, ``18s`` — curto o bastante para uma linha."""
    total = int(max(duracao.total_seconds(), 0))
    dias, resto = divmod(total, 86400)
    horas, resto = divmod(resto, 3600)
    minutos, segundos = divmod(resto, 60)
    if dias:
        return f"{dias}d {horas}h"
    if horas:
        return f"{horas}h{minutos:02d}m"
    if minutos:
        return f"{minutos}min"
    return f"{segundos}s"


def formatar_latencia(segundos: float | None) -> str:
    """A latência do gateway vem NaN enquanto o bot não conectou."""
    if segundos is None or math.isnan(segundos) or math.isinf(segundos):
        return "latência: —"
    return f"latência: {segundos * 1000:.0f} ms"


@dataclass(frozen=True, slots=True)
class Vida:
    """Retrato do bot no instante do log."""

    conectado: bool
    usuario: str
    guilds: int
    latencia: float | None
    no_ar: timedelta
    assinaturas: int
    assinaturas_com_falha: int
    lacos: tuple[EstadoLaco, ...] = ()

    @property
    def saudavel(self) -> bool:
        """Se está tudo como deveria: conectado e com todos os laços girando."""
        return self.conectado and all(laco.ok for laco in self.lacos)

    @property
    def problemas(self) -> tuple[str, ...]:
        achados = []
        if not self.conectado:
            achados.append("o bot não está conectado ao Discord")
        achados.extend(laco.descricao for laco in self.lacos if not laco.ok)
        return tuple(achados)

    def resumo(self) -> str:
        """A linha única que vai para o log."""
        girando = sum(1 for laco in self.lacos if laco.ok)
        estado = f"conectado como {self.usuario}" if self.conectado else "DESCONECTADO"
        falhando = (
            f" ({self.assinaturas_com_falha} em backoff)"
            if self.assinaturas_com_falha
            else ""
        )
        return (
            f"vida | {estado}"
            f" | {self.guilds} guild(s)"
            f" | {formatar_latencia(self.latencia)}"
            f" | no ar há {formatar_duracao(self.no_ar)}"
            f" | assinaturas: {self.assinaturas}{falhando}"
            f" | laços: {girando}/{len(self.lacos)} rodando"
        )


def lacos_do_cog(cog: commands.Cog) -> list[tuple[str, tasks.Loop]]:
    """Os ``tasks.Loop`` de um cog, já ligados à instância.

    A varredura é feita na classe e não com ``dir()`` sobre a instância de
    propósito: ``dir()`` levaria a ler todas as properties, e pelo menos uma
    delas (a sessão HTTP do cog de alertas) levanta exceção quando o bot ainda
    não subiu. Aqui só os nomes que já são ``Loop`` na classe são lidos, e o
    ``getattr`` devolve a cópia que o próprio cog iniciou.
    """
    encontrados = []
    for nome, valor in vars(type(cog)).items():
        if isinstance(valor, tasks.Loop):
            ligado = getattr(cog, nome, None)
            if isinstance(ligado, tasks.Loop):
                encontrados.append((nome, ligado))
    return encontrados


# ---------------------------------------------------------------------------
# Banco
# ---------------------------------------------------------------------------


async def contar_assinaturas(db: Database) -> tuple[int, int]:
    """``(total, em backoff)``. ``(0, 0)`` se a tabela ainda não existe."""
    try:
        row = await db.fetchone(
            "SELECT COUNT(*) AS total,"
            " SUM(CASE WHEN falhas > 0 THEN 1 ELSE 0 END) AS falhando"
            " FROM alertas_assinaturas"
        )
    except Exception:
        log.debug("Não consegui contar as assinaturas", exc_info=True)
        return 0, 0
    if row is None:
        return 0, 0
    return int(row["total"] or 0), int(row["falhando"] or 0)


# ---------------------------------------------------------------------------
# Cog
# ---------------------------------------------------------------------------


class Saude(commands.Cog):
    """O batimento de uma hora."""

    def __init__(self, bot: LeviathanBot) -> None:
        self.bot = bot
        self.db = bot.db
        # monotonic porque o que interessa é duração, e ela não pode andar para
        # trás se o relógio do servidor for ajustado.
        self.inicio = time.monotonic()

    async def cog_load(self) -> None:
        self.batimento.start()

    async def cog_unload(self) -> None:
        self.batimento.cancel()

    def coletar_lacos(self) -> tuple[EstadoLaco, ...]:
        estados = []
        for nome_cog, cog in sorted(self.bot.cogs.items()):
            for nome, laco in lacos_do_cog(cog):
                if laco is self.batimento:
                    continue  # se este aqui não estivesse rodando, não haveria log
                estados.append(
                    EstadoLaco(
                        nome=f"{nome_cog.lower()}.{nome}",
                        rodando=laco.is_running(),
                        falhou=laco.failed(),
                    )
                )
        return tuple(estados)

    async def coletar(self) -> Vida:
        total, falhando = await contar_assinaturas(self.db)
        usuario = str(self.bot.user) if self.bot.user else "?"
        return Vida(
            conectado=self.bot.is_ready() and not self.bot.is_closed(),
            usuario=usuario,
            guilds=len(self.bot.guilds),
            latencia=self.bot.latency,
            no_ar=timedelta(seconds=time.monotonic() - self.inicio),
            assinaturas=total,
            assinaturas_com_falha=falhando,
            lacos=self.coletar_lacos(),
        )

    @tasks.loop(hours=INTERVALO_HORAS)
    async def batimento(self) -> None:
        try:
            vida = await self.coletar()
        except Exception:
            log.exception("Falha ao montar o log de vida")
            return

        log.info("%s", vida.resumo())
        for problema in vida.problemas:
            log.warning("vida | %s", problema)

    @batimento.before_loop
    async def _antes(self) -> None:
        await self.bot.wait_until_ready()


async def setup(bot: LeviathanBot) -> None:
    await bot.add_cog(Saude(bot))
