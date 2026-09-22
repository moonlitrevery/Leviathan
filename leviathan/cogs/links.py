"""Comandos que respondem links úteis fixos.

O conteúdo vem de ``leviathan/data/links.json``, lido quando o cog é carregado —
inclusive num ``/reload links``, já que recarregar a extensão reimporta o módulo.
A ideia é editar o JSON e recarregar, sem tocar em código.

Se o JSON estiver ausente ou malformado, o carregamento levanta
:class:`LinksConfigError` depois de logar o motivo. O ``setup_hook`` do bot trata
cada cog separadamente, então só este fica de fora: o bot sobe mesmo assim.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands

from leviathan.bot import LeviathanBot

log = logging.getLogger(__name__)

#: Fonte de dados dos comandos, versionada no git.
LINKS_PATH = Path(__file__).resolve().parent.parent / "data" / "links.json"

COR_PADRAO = discord.Color.blurple()
RODAPE = "Leviathan · links curados"


class LinksConfigError(RuntimeError):
    """``links.json`` ausente, malformado ou fora do formato esperado."""


# ---------------------------------------------------------------------------
# Modelo
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LinkItem:
    nome: str
    url: str
    descricao: str


@dataclass(frozen=True, slots=True)
class LinkSection:
    """Um bloco de links: o que vira um embed."""

    nome: str  # rótulo curto, usado na escolha do /sistema
    titulo: str
    descricao: str
    cor: discord.Color
    itens: tuple[LinkItem, ...]


@dataclass(frozen=True, slots=True)
class LinksData:
    pirata: LinkSection
    sistema: LinkSection  # cabeçalho da listagem, sem itens próprios
    sistemas: dict[str, LinkSection]


# ---------------------------------------------------------------------------
# Leitura e validação do JSON
# ---------------------------------------------------------------------------


def _objeto(valor: Any, caminho: str) -> dict[str, Any]:
    if not isinstance(valor, dict):
        raise LinksConfigError(f"{caminho} precisa ser um objeto JSON")
    return valor


def _obrigatorio(dados: dict[str, Any], chave: str, caminho: str) -> Any:
    if chave not in dados:
        raise LinksConfigError(f"{caminho}.{chave} é obrigatório")
    return dados[chave]


def _texto(dados: dict[str, Any], chave: str, caminho: str, *, padrao: str | None = None) -> str:
    valor = dados.get(chave)
    if valor is None:
        if padrao is None:
            raise LinksConfigError(f"{caminho}.{chave} é obrigatório")
        return padrao
    if not isinstance(valor, str) or not valor.strip():
        raise LinksConfigError(f"{caminho}.{chave} precisa ser um texto não vazio")
    return valor.strip()


def _cor(dados: dict[str, Any], caminho: str) -> discord.Color:
    bruto = dados.get("cor")
    if bruto is None:
        return COR_PADRAO
    if not isinstance(bruto, str):
        raise LinksConfigError(f"{caminho}.cor precisa ser um texto tipo \"#1793d1\"")
    try:
        return discord.Color(int(bruto.lstrip("#"), 16))
    except ValueError as exc:
        raise LinksConfigError(
            f"{caminho}.cor precisa ser um hexadecimal tipo \"#1793d1\", recebido {bruto!r}"
        ) from exc


def _itens(bruto: Any, caminho: str) -> tuple[LinkItem, ...]:
    if not isinstance(bruto, list):
        raise LinksConfigError(f"{caminho} precisa ser uma lista")
    if not bruto:
        raise LinksConfigError(f"{caminho} está vazio")

    itens = []
    for indice, cru in enumerate(bruto):
        item_caminho = f"{caminho}[{indice}]"
        item = _objeto(cru, item_caminho)
        url = _texto(item, "url", item_caminho)
        if not url.startswith(("http://", "https://")):
            raise LinksConfigError(
                f"{item_caminho}.url precisa começar com http:// ou https://, recebido {url!r}"
            )
        itens.append(
            LinkItem(
                nome=_texto(item, "nome", item_caminho),
                url=url,
                descricao=_texto(item, "descricao", item_caminho, padrao=""),
            )
        )
    return tuple(itens)


def _secao(bruto: Any, caminho: str, *, exige_itens: bool = True) -> LinkSection:
    dados = _objeto(bruto, caminho)
    titulo = _texto(dados, "titulo", caminho)
    return LinkSection(
        nome=_texto(dados, "nome", caminho, padrao=titulo),
        titulo=titulo,
        descricao=_texto(dados, "descricao", caminho, padrao=""),
        cor=_cor(dados, caminho),
        itens=_itens(_obrigatorio(dados, "itens", caminho), f"{caminho}.itens")
        if exige_itens
        else (),
    )


def load_links(path: Path = LINKS_PATH) -> LinksData:
    """Lê e valida o JSON. Levanta :class:`LinksConfigError` com o caminho do erro."""
    try:
        bruto = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise LinksConfigError(f"{path} não existe") from exc
    except OSError as exc:
        raise LinksConfigError(f"não consegui ler {path}: {exc}") from exc

    try:
        dados = json.loads(bruto)
    except json.JSONDecodeError as exc:
        raise LinksConfigError(
            f"{path.name} não é um JSON válido: {exc.msg}"
            f" (linha {exc.lineno}, coluna {exc.colno})"
        ) from exc

    raiz = _objeto(dados, path.name)

    pirata = _secao(_obrigatorio(raiz, "pirata", path.name), f"{path.name}.pirata")

    bruto_sistema = _objeto(
        _obrigatorio(raiz, "sistema", path.name), f"{path.name}.sistema"
    )
    sistema = _secao(bruto_sistema, f"{path.name}.sistema", exige_itens=False)

    caminho_opcoes = f"{path.name}.sistema.opcoes"
    opcoes = _objeto(_obrigatorio(bruto_sistema, "opcoes", f"{path.name}.sistema"), caminho_opcoes)
    if not opcoes:
        raise LinksConfigError(f"{caminho_opcoes} precisa ter ao menos um sistema")

    sistemas = {
        chave: _secao(valor, f"{caminho_opcoes}.{chave}") for chave, valor in opcoes.items()
    }
    return LinksData(pirata=pirata, sistema=sistema, sistemas=sistemas)


def montar_embed(secao: LinkSection, comando: str) -> discord.Embed:
    """Transforma uma seção num embed, com cor da categoria e rodapé discreto."""
    blocos = [secao.descricao] if secao.descricao else []
    for item in secao.itens:
        linha = f"**[{item.nome}]({item.url})**"
        if item.descricao:
            linha += f"\n{item.descricao}"
        blocos.append(linha)

    embed = discord.Embed(
        title=secao.titulo,
        description="\n\n".join(blocos)[:4096],
        color=secao.cor,
    )
    embed.set_footer(text=f"{RODAPE} · /{comando}")
    return embed


# Carregado na importação do módulo, e não dentro do cog, porque as opções do
# /sistema viram Choice no momento em que a classe é definida. Como /reload
# reimporta o módulo, editar o JSON e recarregar continua bastando.
try:
    DADOS = load_links()
except LinksConfigError as exc:
    log.error("Cog de links não carregado — %s", exc)
    raise

SISTEMA_CHOICES = [
    app_commands.Choice(name=secao.nome, value=chave)
    for chave, secao in DADOS.sistemas.items()
][:25]  # limite do Discord


# ---------------------------------------------------------------------------
# Cog
# ---------------------------------------------------------------------------


class Links(commands.Cog):
    """Links úteis fixos, lidos do JSON."""

    def __init__(self, bot: LeviathanBot, dados: LinksData) -> None:
        self.bot = bot
        self.dados = dados

    @app_commands.command(
        name="pirata",
        description="Megathread do r/Piracy e os guias do FMHY.",
    )
    async def pirata(self, interaction: discord.Interaction) -> None:
        # Resposta pública: a graça é compartilhar no canal.
        await interaction.response.send_message(embed=montar_embed(self.dados.pirata, "pirata"))

    @app_commands.command(
        name="sistema",
        description="Links de instalação e formatação de um sistema operacional.",
    )
    @app_commands.describe(distro="Deixe em branco para ver todos os sistemas disponíveis")
    @app_commands.choices(distro=SISTEMA_CHOICES)
    async def sistema(
        self,
        interaction: discord.Interaction,
        distro: str | None = None,
    ) -> None:
        if distro is None:
            await interaction.response.send_message(embed=self._embed_indice())
            return

        secao = self.dados.sistemas.get(distro)
        if secao is None:
            disponiveis = ", ".join(s.nome for s in self.dados.sistemas.values())
            await interaction.response.send_message(
                f"Não tenho links para `{distro}`. Disponíveis: {disponiveis}.",
                ephemeral=True,
            )
            return

        await interaction.response.send_message(embed=montar_embed(secao, f"sistema {distro}"))

    def _embed_indice(self) -> discord.Embed:
        indice = self.dados.sistema
        linhas = "\n".join(f"• **{secao.nome}**" for secao in self.dados.sistemas.values())

        embed = discord.Embed(
            title=indice.titulo,
            description=f"{indice.descricao}\n\n{linhas}" if indice.descricao else linhas,
            color=indice.cor,
        )
        embed.set_footer(text=f"{RODAPE} · /sistema")
        return embed


async def setup(bot: LeviathanBot) -> None:
    await bot.add_cog(Links(bot, DADOS))
