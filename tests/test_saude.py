"""Testes do log de vida.

O valor dessa linha é ser confiável quando ninguém está olhando, então o que
está coberto é a formatação (que vai para o log) e a descoberta dos laços — a
parte que precisa continuar funcionando quando um cog novo aparecer.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from leviathan.cogs.saude import (
    EstadoLaco,
    Vida,
    contar_assinaturas,
    formatar_duracao,
    formatar_latencia,
    lacos_do_cog,
)
from leviathan.db import Database, init_db


@pytest.fixture
async def db(tmp_path):
    database = Database(tmp_path / "teste.db", pool_size=2)
    await database.connect()
    await init_db(database)
    yield database
    await database.close()


def vida(**campos) -> Vida:
    base = dict(
        conectado=True,
        usuario="Leviathan#0001",
        guilds=1,
        latencia=0.048,
        no_ar=timedelta(hours=3, minutes=12),
        assinaturas=7,
        assinaturas_com_falha=0,
        lacos=(EstadoLaco("alertas.ciclo", rodando=True, falhou=False),),
    )
    base.update(campos)
    return Vida(**base)


# ---------------------------------------------------------------------------
# Formatação
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("duracao", "esperado"),
    [
        (timedelta(seconds=18), "18s"),
        (timedelta(minutes=47), "47min"),
        (timedelta(hours=5, minutes=12), "5h12m"),
        (timedelta(hours=5, minutes=2), "5h02m"),
        (timedelta(days=3, hours=4), "3d 4h"),
    ],
)
def test_duracao_fica_curta(duracao, esperado):
    assert formatar_duracao(duracao) == esperado


def test_duracao_negativa_nao_vira_lixo():
    """Relógio ajustado para trás não pode produzir 'no ar há -1d'."""
    assert formatar_duracao(timedelta(seconds=-5)) == "0s"


def test_latencia_em_milissegundos():
    assert formatar_latencia(0.048) == "latência: 48 ms"


@pytest.mark.parametrize("valor", [None, float("nan"), float("inf")])
def test_latencia_indisponivel_nao_explode(valor):
    """Antes de conectar, discord.py devolve NaN em bot.latency."""
    assert formatar_latencia(valor) == "latência: —"


# ---------------------------------------------------------------------------
# A linha do log
# ---------------------------------------------------------------------------


def test_resumo_tem_o_que_precisa_estar_la():
    linha = vida().resumo()

    assert "conectado como Leviathan#0001" in linha
    assert "1 guild(s)" in linha
    assert "48 ms" in linha
    assert "no ar há 3h12m" in linha
    assert "assinaturas: 7" in linha
    assert "laços: 1/1 rodando" in linha


def test_resumo_cabe_em_uma_linha():
    assert "\n" not in vida().resumo()


def test_assinaturas_em_backoff_aparecem():
    assert "(2 em backoff)" in vida(assinaturas_com_falha=2).resumo()


def test_sem_backoff_nao_polui_a_linha():
    assert "backoff" not in vida(assinaturas_com_falha=0).resumo()


def test_desconectado_grita_no_resumo():
    linha = vida(conectado=False).resumo()

    assert "DESCONECTADO" in linha


def test_laco_parado_entra_na_conta():
    parados = (
        EstadoLaco("alertas.ciclo", rodando=True, falhou=False),
        EstadoLaco("quemfalou.agendador", rodando=False, falhou=False),
    )

    assert "laços: 1/2 rodando" in vida(lacos=parados).resumo()


# ---------------------------------------------------------------------------
# Saúde e problemas
# ---------------------------------------------------------------------------


def test_tudo_certo_nao_tem_problema():
    atual = vida()

    assert atual.saudavel is True
    assert atual.problemas == ()


def test_desconectado_e_problema():
    atual = vida(conectado=False)

    assert atual.saudavel is False
    assert "não está conectado" in atual.problemas[0]


def test_laco_que_morreu_com_erro_e_problema():
    """Um laço que levantou exceção para de girar calado; é o pior caso."""
    atual = vida(lacos=(EstadoLaco("alertas.ciclo", rodando=False, falhou=True),))

    assert atual.saudavel is False
    assert atual.problemas == ("alertas.ciclo: PAROU COM ERRO",)


def test_laco_parado_sem_erro_tambem_e_problema():
    atual = vida(lacos=(EstadoLaco("quemfalou.agendador", rodando=False, falhou=False),))

    assert atual.problemas == ("quemfalou.agendador: parado",)


def test_varios_problemas_sao_listados():
    atual = vida(
        conectado=False,
        lacos=(
            EstadoLaco("a", rodando=True, falhou=False),
            EstadoLaco("b", rodando=False, falhou=False),
        ),
    )

    assert len(atual.problemas) == 2


# ---------------------------------------------------------------------------
# Descoberta dos laços nos cogs de verdade
# ---------------------------------------------------------------------------


def test_encontra_os_lacos_dos_cogs_reais():
    """Os laços são descobertos na classe, sem instanciar cog nem tocar no bot.

    Se um cog novo ganhar um tasks.loop, ele entra no log de vida sozinho — e se
    alguém renomear um laço existente, este teste avisa.
    """
    from leviathan.cogs.alertas import Alertas
    from leviathan.cogs.quemfalou import QuemFalou

    alertas = {nome for nome, _ in lacos_do_cog(_instancia_crua(Alertas))}
    quemfalou = {nome for nome, _ in lacos_do_cog(_instancia_crua(QuemFalou))}

    assert alertas == {"ciclo"}
    assert quemfalou == {"agendador", "verificar_expiradas"}


def test_cog_sem_laco_nenhum_devolve_lista_vazia():
    from leviathan.cogs.links import Links

    assert lacos_do_cog(_instancia_crua(Links)) == []


def _instancia_crua(classe):
    """Instância sem ``__init__``: só serve para ler os descritores da classe."""
    return object.__new__(classe)


# ---------------------------------------------------------------------------
# Contagem de assinaturas
# ---------------------------------------------------------------------------


async def test_banco_vazio_conta_zero(db):
    assert await contar_assinaturas(db) == (0, 0)


async def test_conta_total_e_quantas_estao_falhando(db):
    from leviathan.cogs.alertas import criar_assinatura, registrar_checagem
    from datetime import datetime, timezone

    for n in range(3):
        assinatura = await criar_assinatura(
            db,
            guild_id=1,
            tipo="rss",
            externo_id=f"https://ex.com/{n}",
            nome=f"Feed {n}",
            url="",
            channel_id=1,
            cargo_id=None,
        )
        assert assinatura is not None
        if n == 0:
            await registrar_checagem(
                db,
                assinatura.id,
                falhou=True,
                proxima=datetime.now(timezone.utc),
            )

    assert await contar_assinaturas(db) == (3, 1)
