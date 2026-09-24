"""Testes do cog de alertas.

O que dá para testar sem rede está testado aqui: o parse de um feed real do
YouTube guardado em ``tests/fixtures``, o agrupamento de capítulos do MangaDex,
a conta do backoff, a poda dos ids vistos e — a regra que mais importa — o
cadastro que marca tudo como visto sem notificar ninguém.

As chamadas HTTP não aparecem: o cog é exercitado por uma subclasse que troca
``coletar`` por uma lista fixa e guarda o que teria sido notificado.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

import discord
import pytest

from leviathan.cogs.alertas import (
    BACKOFF_MAX,
    LIMITE_ITENS,
    LIMITE_VISTOS,
    MAX_EMBEDS_SEPARADOS,
    TIPO_RSS,
    TIPO_TWITCH,
    TIPO_YOUTUBE,
    Alertas,
    Assinatura,
    Entrega,
    FonteIndisponivel,
    Item,
    agrupar_capitulos,
    calcular_backoff,
    criar_assinatura,
    extrair_channel_id,
    get_assinatura,
    ids_vistos,
    interpretar_alvo_twitch,
    interpretar_alvo_youtube,
    live_para_item,
    marcar_vistos,
    parse_feed,
    podar_vistos,
    separar_novidades,
    thumbnail_da_live,
)
from leviathan.db import Database, init_db

GUILD = 111
CANAL = 222
FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
async def db(tmp_path):
    database = Database(tmp_path / "teste.db", pool_size=2)
    await database.connect()
    await init_db(database)
    yield database
    await database.close()


def item(identificador: str, titulo: str = "algum título") -> Item:
    return Item(id=identificador, titulo=titulo, url=f"https://ex.com/{identificador}")


class CogDeTeste(Alertas):
    """O cog sem Discord nem rede: a fonte é uma lista, o aviso é uma lista.

    ``__init__`` não chama o da classe base de propósito — o que interessa aqui
    é só o banco e o fluxo de processar/primeira_carga.
    """

    def __init__(self, database: Database, itens) -> None:
        self.db = database
        self.itens = itens
        self.avisados: list[list[Item]] = []
        #: Quantos itens de cada aviso "chegam" ao canal. None = todos.
        self.entrega_ate: int | None = None
        self.erro_entrega: str | None = None

    async def coletar(self, assinatura):
        if isinstance(self.itens, Exception):
            raise self.itens
        return list(self.itens)

    async def notificar(self, assinatura, itens):
        self.avisados.append(list(itens))
        if self.erro_entrega is None:
            return Entrega(entregues=tuple(itens))
        entregues = tuple(itens[: self.entrega_ate or 0])
        return Entrega(entregues=entregues, erro=self.erro_entrega)


async def assinar(database: Database, tipo: str = TIPO_RSS):
    assinatura = await criar_assinatura(
        database,
        guild_id=GUILD,
        tipo=tipo,
        externo_id="https://exemplo.com/feed.xml",
        nome="Feed de exemplo",
        url="https://exemplo.com/feed.xml",
        channel_id=CANAL,
        cargo_id=None,
    )
    assert assinatura is not None
    return assinatura


# ---------------------------------------------------------------------------
# Parse do feed do YouTube
# ---------------------------------------------------------------------------


def test_parse_do_feed_real_do_youtube():
    """Feed do canal Linus Tech Tips, baixado de verdade e guardado como fixture."""
    xml = (FIXTURES / "youtube_feed.xml").read_text(encoding="utf-8")

    titulo, itens = parse_feed(xml)

    assert titulo == "Linus Tech Tips"
    assert len(itens) == 15, "o feed do YouTube traz sempre os 15 últimos vídeos"


def test_item_do_youtube_tem_o_que_o_alerta_precisa():
    xml = (FIXTURES / "youtube_feed.xml").read_text(encoding="utf-8")

    primeiro = parse_feed(xml)[1][0]

    assert primeiro.id == "GaX0frxNwfQ", "o id é o do vídeo, não o 'yt:video:...'"
    assert primeiro.url == "https://www.youtube.com/watch?v=GaX0frxNwfQ"
    assert primeiro.titulo
    assert primeiro.thumbnail and primeiro.thumbnail.startswith("https://")
    assert primeiro.publicado_em is not None
    assert primeiro.publicado_em.tzinfo is not None, "data sem fuso quebra o embed"


def test_ids_do_feed_sao_unicos():
    """São eles que deduplicam os alertas; id repetido esconderia vídeo novo."""
    _, itens = parse_feed((FIXTURES / "youtube_feed.xml").read_text(encoding="utf-8"))

    assert len({i.id for i in itens}) == len(itens)


def test_feed_vazio_nao_explode():
    titulo, itens = parse_feed("<rss><channel><title>Nada</title></channel></rss>")

    assert titulo == "Nada"
    assert itens == []


def test_texto_que_nao_e_feed_nao_explode():
    titulo, itens = parse_feed("isso aqui não é um feed")

    assert (titulo, itens) == ("", [])


# ---------------------------------------------------------------------------
# Resolução do canal do YouTube
# ---------------------------------------------------------------------------


def test_channel_id_vem_do_canonical_e_nao_do_primeiro_channelid():
    """Regressão: na página de um canal, "channelId" aparece dezenas de vezes.

    As primeiras ocorrências são de canais recomendados na barra lateral. Usar a
    primeira faria o bot assinar, calado, um canal que ninguém pediu.
    """
    html = (
        '<script>{"channelId":"UCrecomendado000000000aa"}</script>'
        '<link rel="canonical" href="https://www.youtube.com/channel/UCcerto00000000000000000">'
    )

    assert extrair_channel_id(html) == "UCcerto00000000000000000"


def test_channel_id_cai_no_itemprop_quando_nao_ha_canonical():
    html = '<meta itemprop="identifier" content="UCcerto00000000000000000">'

    assert extrair_channel_id(html) == "UCcerto00000000000000000"


def test_pagina_sem_id_devolve_nada():
    assert extrair_channel_id("<html>página de consentimento</html>") is None


@pytest.mark.parametrize(
    "entrada",
    [
        "UCXuqSBlHAE6Xw-yeJA0Tunw",
        "https://www.youtube.com/channel/UCXuqSBlHAE6Xw-yeJA0Tunw",
        "youtube.com/channel/UCXuqSBlHAE6Xw-yeJA0Tunw",
    ],
)
def test_id_do_canal_e_reconhecido_direto(entrada):
    assert interpretar_alvo_youtube(entrada) == ("id", "UCXuqSBlHAE6Xw-yeJA0Tunw")


@pytest.mark.parametrize(
    "entrada",
    ["@LinusTechTips", "LinusTechTips", "https://www.youtube.com/@LinusTechTips"],
)
def test_handle_vira_pagina_a_baixar(entrada):
    tipo, url = interpretar_alvo_youtube(entrada)

    assert tipo == "pagina"
    assert url.startswith("https://www.youtube.com/")


def test_entrada_sem_sentido_e_recusada():
    with pytest.raises(FonteIndisponivel):
        interpretar_alvo_youtube("!!!")


# ---------------------------------------------------------------------------
# Agrupamento de capítulos do MangaDex
# ---------------------------------------------------------------------------


def capitulo(uuid: str, numero, idioma: str, publicado: str, titulo: str = ""):
    return {
        "id": uuid,
        "attributes": {
            "chapter": numero,
            "title": titulo,
            "translatedLanguage": idioma,
            "publishAt": publicado,
        },
    }


def test_mesmo_capitulo_em_dois_idiomas_e_dois_grupos_vira_um_alerta():
    """Um capítulo sai várias vezes no MangaDex; o alerta é um só."""
    dados = [
        capitulo("aaa", "1193", "pt-br", "2026-09-13T15:11:04+00:00"),
        capitulo("bbb", "1193", "en", "2026-09-13T15:10:16+00:00"),
        capitulo("ccc", "1193", "en", "2026-09-13T12:00:00+00:00"),
        capitulo("ddd", "1193", "es", "2026-09-13T11:00:00+00:00"),
    ]

    itens = agrupar_capitulos(dados)

    assert len(itens) == 1
    assert itens[0].id == "cap:1193"
    assert itens[0].detalhe == "en, es, pt-br", "os idiomas viram um detalhe só"


def test_capitulos_diferentes_continuam_separados():
    dados = [
        capitulo("aaa", "1193", "en", "2026-09-13T15:00:00+00:00"),
        capitulo("bbb", "1192", "en", "2026-09-06T15:00:00+00:00"),
        capitulo("ccc", "1192", "pt-br", "2026-09-06T15:20:00+00:00"),
    ]

    itens = agrupar_capitulos(dados)

    assert [i.id for i in itens] == ["cap:1193", "cap:1192"], "do mais novo ao mais velho"


def test_link_do_grupo_aponta_para_o_upload_mais_recente():
    dados = [
        capitulo("antigo", "50", "en", "2026-01-01T00:00:00+00:00"),
        capitulo("recente", "50", "pt-br", "2026-02-01T00:00:00+00:00"),
    ]

    assert agrupar_capitulos(dados)[0].url.endswith("/recente")


def test_capitulo_sem_numero_usa_o_id_do_upload():
    """Oneshot e extra não têm número; agrupar por número juntaria coisas distintas."""
    dados = [
        capitulo("aaa", None, "en", "2026-01-01T00:00:00+00:00"),
        capitulo("bbb", None, "en", "2026-02-01T00:00:00+00:00"),
    ]

    itens = agrupar_capitulos(dados)

    assert {i.id for i in itens} == {"id:aaa", "id:bbb"}


def test_titulo_do_capitulo_entra_no_alerta():
    dados = [capitulo("aaa", "7", "en", "2026-01-01T00:00:00+00:00", titulo="O começo")]

    assert agrupar_capitulos(dados)[0].titulo == "Capítulo 7 — O começo"


def test_lista_vazia_do_mangadex_nao_explode():
    assert agrupar_capitulos([]) == []


# ---------------------------------------------------------------------------
# Backoff
# ---------------------------------------------------------------------------

BASE = timedelta(minutes=10)


def test_sem_falha_vale_o_intervalo_normal():
    assert calcular_backoff(0, BASE) == BASE


def test_cada_falha_dobra_a_espera():
    assert calcular_backoff(1, BASE) == timedelta(minutes=20)
    assert calcular_backoff(2, BASE) == timedelta(minutes=40)
    assert calcular_backoff(3, BASE) == timedelta(minutes=80)


def test_backoff_para_de_crescer_no_teto():
    assert calcular_backoff(20, BASE) == BACKOFF_MAX
    assert calcular_backoff(1000, BASE) == BACKOFF_MAX, "nem com número absurdo estoura"


def test_backoff_nunca_diminui():
    esperas = [calcular_backoff(n, BASE) for n in range(0, 15)]

    assert esperas == sorted(esperas)


def test_falha_negativa_e_tratada_como_zero():
    assert calcular_backoff(-1, BASE) == BASE


# ---------------------------------------------------------------------------
# Novidades e a regra do cadastro
# ---------------------------------------------------------------------------


def test_cadastro_nao_notifica_nada_mas_marca_tudo():
    """Sem isso, assinar um canal com 15 vídeos despejaria 15 alertas."""
    itens = [item(f"v{n}") for n in range(15)]

    avisar, marcar = separar_novidades(itens, set(), notificar=False)

    assert avisar == []
    assert sorted(marcar) == sorted(i.id for i in itens)


def test_checagem_normal_avisa_só_o_que_e_novo():
    itens = [item("novo"), item("velho")]

    avisar, marcar = separar_novidades(itens, {"velho"}, notificar=True)

    assert [i.id for i in avisar] == ["novo"]
    assert marcar == ["novo"]


def test_novidades_saem_do_mais_antigo_para_o_mais_novo():
    """As fontes devolvem do mais novo primeiro; no canal a ordem se inverte."""
    itens = [item("terceiro"), item("segundo"), item("primeiro")]

    avisar, _ = separar_novidades(itens, set(), notificar=True)

    assert [i.id for i in avisar] == ["primeiro", "segundo", "terceiro"]


def test_nada_novo_nao_gera_aviso():
    itens = [item("a"), item("b")]

    assert separar_novidades(itens, {"a", "b"}, notificar=True) == ([], [])


async def test_primeira_carga_nao_avisa_ninguem(db):
    assinatura = await assinar(db)
    cog = CogDeTeste(db, [item(f"v{n}") for n in range(15)])

    marcados = await cog.primeira_carga(assinatura)

    assert cog.avisados == [], "o cadastro não pode notificar nada"
    assert marcados == 15
    assert len(await ids_vistos(db, assinatura.id)) == 15


async def test_depois_do_cadastro_so_o_item_novo_vira_alerta(db):
    assinatura = await assinar(db)
    antigos = [item(f"v{n}") for n in range(15)]
    cog = CogDeTeste(db, antigos)
    await cog.primeira_carga(assinatura)

    cog.itens = [item("recem-saido"), *antigos]
    await cog.processar(assinatura)

    assert [i.id for i in cog.avisados[0]] == ["recem-saido"]
    assert len(cog.avisados) == 1


async def test_segunda_checagem_sem_novidade_fica_calada(db):
    assinatura = await assinar(db)
    cog = CogDeTeste(db, [item("a"), item("b")])
    await cog.primeira_carga(assinatura)

    await cog.processar(assinatura)

    assert cog.avisados == []


async def test_fonte_que_falha_conta_falha_e_adia(db):
    assinatura = await assinar(db)
    cog = CogDeTeste(db, FonteIndisponivel("caiu"))

    assert await cog.processar(assinatura) == []

    depois = await get_assinatura(db, assinatura.id)
    assert depois is not None
    assert depois.falhas == 1
    assert depois.proxima_checagem > datetime.now(timezone.utc)


async def test_sucesso_zera_as_falhas(db):
    assinatura = await assinar(db)
    cog = CogDeTeste(db, FonteIndisponivel("caiu"))
    await cog.processar(assinatura)
    await cog.processar(await get_assinatura(db, assinatura.id))

    cog.itens = [item("voltou")]
    await cog.processar(await get_assinatura(db, assinatura.id))

    depois = await get_assinatura(db, assinatura.id)
    assert depois is not None and depois.falhas == 0


async def test_falha_no_cadastro_nao_impede_a_assinatura(db):
    """A fonte pode estar fora do ar bem na hora; o laço resolve depois."""
    assinatura = await assinar(db)
    cog = CogDeTeste(db, FonteIndisponivel("fora do ar"))

    assert await cog.primeira_carga(assinatura) == 0
    assert await get_assinatura(db, assinatura.id) is not None


async def test_cadastro_com_a_fonte_caida_deixa_a_carga_pendente(db):
    assinatura = await assinar(db)
    cog = CogDeTeste(db, FonteIndisponivel("fora do ar"))
    await cog.primeira_carga(assinatura)

    depois = await get_assinatura(db, assinatura.id)

    assert depois is not None
    assert depois.opcoes.get("carga_pendente") is True
    assert depois.falhas == 1, "a fonte caída conta falha e entra em backoff"


async def test_carga_pendente_e_feita_calada_na_primeira_checagem_que_funciona(db):
    """O buraco que isso fecha: assinar com a fonte caída não pode virar enxurrada.

    Sem a marca de carga pendente, a checagem seguinte veria o feed inteiro como
    novidade e despejaria tudo no canal — exatamente o que o cadastro evita.
    """
    assinatura = await assinar(db)
    cog = CogDeTeste(db, FonteIndisponivel("fora do ar"))
    await cog.primeira_carga(assinatura)

    cog.itens = [item(f"v{n}") for n in range(15)]
    await cog.processar(await get_assinatura(db, assinatura.id))

    assert cog.avisados == [], "o feed inteiro não pode virar alerta"
    assert len(await ids_vistos(db, assinatura.id)) == 15

    depois = await get_assinatura(db, assinatura.id)
    assert depois is not None
    assert "carga_pendente" not in depois.opcoes, "a pendência é cobrada uma vez só"


async def test_depois_da_carga_atrasada_o_item_novo_volta_a_avisar(db):
    assinatura = await assinar(db)
    cog = CogDeTeste(db, FonteIndisponivel("fora do ar"))
    await cog.primeira_carga(assinatura)
    cog.itens = [item("velho")]
    await cog.processar(await get_assinatura(db, assinatura.id))

    cog.itens = [item("novo"), item("velho")]
    await cog.processar(await get_assinatura(db, assinatura.id))

    assert [i.id for i in cog.avisados[0]] == ["novo"]


async def test_opcoes_da_assinatura_sobrevivem_a_carga_pendente(db):
    """A pendência não pode apagar ignorar_shorts nem os idiomas escolhidos."""
    assinatura = await criar_assinatura(
        db,
        guild_id=GUILD,
        tipo=TIPO_YOUTUBE,
        externo_id="UCalgumacoisa",
        nome="Canal",
        url="",
        channel_id=CANAL,
        cargo_id=None,
        opcoes={"ignorar_shorts": True},
    )
    assert assinatura is not None
    cog = CogDeTeste(db, FonteIndisponivel("fora do ar"))
    await cog.primeira_carga(assinatura)

    pendente = await get_assinatura(db, assinatura.id)
    assert pendente is not None and pendente.opcoes["ignorar_shorts"] is True

    cog.itens = []
    await cog.processar(pendente)

    final = await get_assinatura(db, assinatura.id)
    assert final is not None
    assert final.opcoes == {"ignorar_shorts": True}


async def test_assinar_a_mesma_fonte_duas_vezes_e_recusado(db):
    await assinar(db)

    repetida = await criar_assinatura(
        db,
        guild_id=GUILD,
        tipo=TIPO_RSS,
        externo_id="https://exemplo.com/feed.xml",
        nome="outro nome",
        url="https://exemplo.com/feed.xml",
        channel_id=CANAL,
        cargo_id=None,
    )
    assert repetida is None


# ---------------------------------------------------------------------------
# Poda dos ids vistos
# ---------------------------------------------------------------------------


async def test_poda_mantem_so_os_200_mais_recentes(db):
    assinatura = await assinar(db, TIPO_YOUTUBE)
    await marcar_vistos(db, assinatura.id, [f"v{n:04d}" for n in range(250)])

    guardados = await ids_vistos(db, assinatura.id)

    assert len(guardados) == LIMITE_VISTOS
    assert "v0249" in guardados, "o mais recente fica"
    assert "v0000" not in guardados, "o mais antigo sai"


async def test_poda_nao_mexe_em_quem_esta_abaixo_do_limite(db):
    assinatura = await assinar(db)
    await marcar_vistos(db, assinatura.id, [f"v{n}" for n in range(10)])

    assert await podar_vistos(db, assinatura.id) == 0
    assert len(await ids_vistos(db, assinatura.id)) == 10


async def test_poda_nao_atinge_outra_assinatura(db):
    uma = await assinar(db, TIPO_YOUTUBE)
    outra = await criar_assinatura(
        db,
        guild_id=GUILD,
        tipo=TIPO_RSS,
        externo_id="https://outra.com/feed",
        nome="Outra",
        url="https://outra.com/feed",
        channel_id=CANAL,
        cargo_id=None,
    )
    assert outra is not None
    await marcar_vistos(db, outra.id, ["intocado"])

    await marcar_vistos(db, uma.id, [f"v{n:04d}" for n in range(300)])

    assert await ids_vistos(db, outra.id) == {"intocado"}


async def test_item_ja_visto_nao_duplica(db):
    assinatura = await assinar(db)
    await marcar_vistos(db, assinatura.id, ["a", "b"])
    await marcar_vistos(db, assinatura.id, ["b", "c"])

    assert await ids_vistos(db, assinatura.id) == {"a", "b", "c"}


def test_feed_gigante_e_cortado_no_teto():
    """Um feed maior que a memória de vistos ressuscitaria itens antigos.

    Se uma checagem trouxesse 300 itens e a poda guardasse só 200, os 100 mais
    antigos voltariam a parecer novidade na checagem seguinte — alerta repetido
    de coisa velha. O corte mantém o que é considerado abaixo do que é guardado.
    """
    entradas = "".join(
        f"<item><guid>i{n}</guid><title>Item {n}</title>"
        f"<link>https://ex.com/{n}</link></item>"
        for n in range(300)
    )
    _, itens = parse_feed(f"<rss><channel><title>Grande</title>{entradas}</channel></rss>")

    assert len(itens) == LIMITE_ITENS
    assert LIMITE_ITENS < LIMITE_VISTOS, "o teto precisa caber na memória de vistos"


async def test_feed_grande_nao_repete_alerta_na_checagem_seguinte(db):
    """O ciclo completo: cadastro silencioso, poda, e nada ressuscitando."""
    assinatura = await assinar(db)
    itens = [item(f"v{n:04d}") for n in range(LIMITE_ITENS)]
    cog = CogDeTeste(db, itens)
    await cog.primeira_carga(assinatura)

    await cog.processar(assinatura)

    assert cog.avisados == [], "nada do feed original pode voltar como novidade"


# ---------------------------------------------------------------------------
# Entrega: nada some em silêncio
# ---------------------------------------------------------------------------


class CanalFalso(discord.abc.Messageable):
    """Canal que registra os envios e pode falhar a partir do n-ésimo."""

    def __init__(self, falha_a_partir_de: int | None = None, erro=None) -> None:
        self.enviados: list[dict] = []
        self.falha_a_partir_de = falha_a_partir_de
        self.erro = erro or discord.HTTPException(_RespostaFalsa(), "deu ruim")

    async def _get_channel(self):
        return self

    async def send(self, **kwargs):
        if (
            self.falha_a_partir_de is not None
            and len(self.enviados) >= self.falha_a_partir_de
        ):
            raise self.erro
        self.enviados.append(kwargs)
        return None


class _RespostaFalsa:
    status = 500
    reason = "Internal Server Error"


class BotFalso:
    def __init__(self, canal, pronto: bool = True) -> None:
        self.canal = canal
        self.pronto = pronto

    def get_channel(self, channel_id):
        return self.canal

    def is_ready(self):
        return self.pronto

    def is_closed(self):
        return False


class CogComCanal(Alertas):
    """Exercita o notificar() de verdade, com um canal que pode falhar."""

    def __init__(self, database: Database, itens, canal, pronto: bool = True) -> None:
        self.db = database
        self.itens = itens
        self.bot = BotFalso(canal, pronto)

    async def coletar(self, assinatura):
        if isinstance(self.itens, Exception):
            raise self.itens
        return list(self.itens)


async def test_envio_que_falha_nao_marca_nada_como_visto(db):
    """O buraco: item marcado sem ter sido entregue nunca mais vira alerta."""
    assinatura = await assinar(db)
    canal = CanalFalso(falha_a_partir_de=0)
    cog = CogComCanal(db, [item("v1")], canal)

    await cog.processar(assinatura)

    assert canal.enviados == []
    assert await ids_vistos(db, assinatura.id) == set(), "nada entregue, nada marcado"


async def test_envio_que_falha_conta_falha_da_assinatura(db):
    assinatura = await assinar(db)
    cog = CogComCanal(db, [item("v1")], CanalFalso(falha_a_partir_de=0))

    await cog.processar(assinatura)

    depois = await get_assinatura(db, assinatura.id)
    assert depois is not None
    assert depois.falhas == 1, "a entrega falha entra no backoff"
    assert depois.proxima_checagem > datetime.now(timezone.utc)


async def test_sem_permissao_tambem_conta_como_falha(db):
    assinatura = await assinar(db)
    proibido = discord.Forbidden(_RespostaFalsa(), "sem permissão")
    cog = CogComCanal(db, [item("v1")], CanalFalso(falha_a_partir_de=0, erro=proibido))

    await cog.processar(assinatura)

    depois = await get_assinatura(db, assinatura.id)
    assert depois is not None and depois.falhas == 1
    assert await ids_vistos(db, assinatura.id) == set()


async def test_checagem_seguinte_reenvia_o_que_faltou(db):
    assinatura = await assinar(db)
    canal = CanalFalso(falha_a_partir_de=0)
    cog = CogComCanal(db, [item("v1")], canal)
    await cog.processar(assinatura)

    canal.falha_a_partir_de = None  # o canal voltou
    await cog.processar(await get_assinatura(db, assinatura.id))

    assert len(canal.enviados) == 1, "o alerta perdido foi reenviado"
    assert await ids_vistos(db, assinatura.id) == {"v1"}
    depois = await get_assinatura(db, assinatura.id)
    assert depois is not None and depois.falhas == 0


async def test_entrega_parcial_marca_so_o_que_saiu(db):
    """Três embeds, o terceiro falha: os dois primeiros já estão no canal."""
    assinatura = await assinar(db)
    itens = [item("v3"), item("v2"), item("v1")]  # a fonte devolve do mais novo
    assert len(itens) <= MAX_EMBEDS_SEPARADOS, "aqui cada item precisa virar um embed"
    canal = CanalFalso(falha_a_partir_de=2)
    cog = CogComCanal(db, itens, canal)

    await cog.processar(assinatura)

    assert len(canal.enviados) == 2
    assert await ids_vistos(db, assinatura.id) == {"v1", "v2"}, (
        "os avisos saem do mais antigo para o mais novo"
    )


async def test_o_que_ficou_de_fora_da_entrega_parcial_sai_depois(db):
    assinatura = await assinar(db)
    canal = CanalFalso(falha_a_partir_de=2)
    cog = CogComCanal(db, [item("v3"), item("v2"), item("v1")], canal)
    await cog.processar(assinatura)

    canal.falha_a_partir_de = None
    await cog.processar(await get_assinatura(db, assinatura.id))

    assert len(canal.enviados) == 3, "o terceiro alerta foi entregue na volta"
    assert await ids_vistos(db, assinatura.id) == {"v1", "v2", "v3"}


async def test_lote_unico_que_falha_nao_marca_nenhum_item(db):
    """Acima de três itens vira um embed só: ou tudo chega, ou nada chega."""
    assinatura = await assinar(db)
    itens = [item(f"v{n}") for n in range(MAX_EMBEDS_SEPARADOS + 2)]
    canal = CanalFalso(falha_a_partir_de=0)
    cog = CogComCanal(db, itens, canal)

    await cog.processar(assinatura)

    assert await ids_vistos(db, assinatura.id) == set()


async def test_entrega_completa_marca_tudo_e_zera_falhas(db):
    assinatura = await assinar(db)
    canal = CanalFalso()
    cog = CogComCanal(db, [item("v2"), item("v1")], canal)

    entregues = await cog.processar(assinatura)

    assert [i.id for i in entregues] == ["v1", "v2"]
    assert await ids_vistos(db, assinatura.id) == {"v1", "v2"}
    depois = await get_assinatura(db, assinatura.id)
    assert depois is not None and depois.falhas == 0


async def test_canal_apagado_marca_como_visto_para_nao_retentar_para_sempre(db):
    assinatura = await assinar(db)
    cog = CogComCanal(db, [item("v1")], canal=None)

    await cog.processar(assinatura)

    assert await ids_vistos(db, assinatura.id) == {"v1"}
    depois = await get_assinatura(db, assinatura.id)
    assert depois is not None and depois.falhas == 0, (
        "canal apagado não é falha da fonte"
    )


async def test_bot_ainda_desconectado_adia_em_vez_de_descartar(db):
    """Cache frio devolve None para canal que existe; descartar seria perder alerta."""
    assinatura = await assinar(db)
    cog = CogComCanal(db, [item("v1")], canal=None, pronto=False)

    await cog.processar(assinatura)

    assert await ids_vistos(db, assinatura.id) == set()
    depois = await get_assinatura(db, assinatura.id)
    assert depois is not None and depois.falhas == 1


async def test_notificar_devolve_o_que_entregou(db):
    assinatura = await assinar(db)
    canal = CanalFalso(falha_a_partir_de=1)
    cog = CogComCanal(db, [], canal)

    entrega = await cog.notificar(assinatura, [item("a"), item("b")])

    assert [i.id for i in entrega.entregues] == ["a"]
    assert entrega.ok is False
    assert entrega.erro and "recusou" in entrega.erro


async def test_entrega_sem_erro_e_ok():
    assert Entrega(entregues=(item("a"),)).ok is True
    assert Entrega(erro="caiu").ok is False


async def test_cargo_e_mencionado_so_na_primeira_mensagem(db):
    """O ping precisa de AllowedMentions explícito porque o bot usa none()."""
    assinatura = await criar_assinatura(
        db,
        guild_id=GUILD,
        tipo=TIPO_RSS,
        externo_id="https://com-cargo/feed",
        nome="Com cargo",
        url="",
        channel_id=CANAL,
        cargo_id=777,
    )
    assert assinatura is not None
    canal = CanalFalso()
    cog = CogComCanal(db, [], canal)

    await cog.notificar(assinatura, [item("a"), item("b")])

    primeiro, segundo = canal.enviados
    assert primeiro["content"] == "<@&777>"
    assert primeiro["allowed_mentions"].to_dict() == {"roles": [777], "parse": []}
    assert segundo["content"] is None
    assert segundo["allowed_mentions"].to_dict() == {"parse": []}


async def test_short_descartado_e_marcado_mesmo_sem_entrega(db):
    """O short não vira aviso, então não há entrega para dar errado."""
    assinatura = await criar_assinatura(
        db,
        guild_id=GUILD,
        tipo=TIPO_YOUTUBE,
        externo_id="UCcanal",
        nome="Canal",
        url="",
        channel_id=CANAL,
        cargo_id=None,
        opcoes={"ignorar_shorts": True},
    )
    assert assinatura is not None

    class CogComShorts(CogComCanal):
        async def e_short(self, video_id: str) -> bool:
            return video_id == "short"

    canal = CanalFalso()
    cog = CogComShorts(db, [item("normal"), item("short")], canal)
    await cog.processar(assinatura)

    assert await ids_vistos(db, assinatura.id) == {"normal", "short"}
    assert len(canal.enviados) == 1, "só o vídeo normal virou alerta"


# ---------------------------------------------------------------------------
# Twitch: o canal informado
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "entrada",
    [
        "gaules",
        "@gaules",
        "Gaules",
        "twitch.tv/gaules",
        "www.twitch.tv/gaules",
        "https://www.twitch.tv/gaules",
        "https://twitch.tv/gaules/videos",
        "  gaules  ",
    ],
)
def test_canal_da_twitch_sai_do_que_a_pessoa_digitar(entrada):
    assert interpretar_alvo_twitch(entrada) == "gaules"


@pytest.mark.parametrize("entrada", ["", "ab", "nome com espaço", "!!!", "@"])
def test_entrada_que_nao_e_canal_da_twitch_e_recusada(entrada):
    with pytest.raises(FonteIndisponivel):
        interpretar_alvo_twitch(entrada)


# ---------------------------------------------------------------------------
# Twitch: a live vira item
# ---------------------------------------------------------------------------


def live(id_stream: str = "4242", **campos):
    base = {
        "id": id_stream,
        "title": "CS2 ranked até o topo",
        "game_name": "Counter-Strike 2",
        "viewer_count": 1200,
        "started_at": "2026-09-23T01:00:00Z",
        "thumbnail_url": "https://x/live_user_gaules-{width}x{height}.jpg",
    }
    base.update(campos)
    return base


def test_id_do_item_e_o_da_transmissao():
    """É isso que faz o aviso sair uma vez por live, e de novo na live seguinte."""
    assert live_para_item(live("999"), login="gaules").id == "live:999"


def test_item_da_live_leva_titulo_jogo_e_audiencia():
    item = live_para_item(live(), login="gaules")

    assert item.titulo == "CS2 ranked até o topo"
    assert item.detalhe == "Counter-Strike 2 · 1200 assistindo"
    assert item.url == "https://www.twitch.tv/gaules"
    assert item.publicado_em is not None


def test_prévia_da_live_troca_os_marcadores_de_tamanho():
    """Mandar {width}x{height} literal para o Discord renderiza imagem quebrada."""
    item = live_para_item(live(), login="gaules")

    assert item.thumbnail == "https://x/live_user_gaules-1280x720.jpg"
    assert "{" not in item.thumbnail


def test_previa_sem_marcadores_passa_intacta():
    assert thumbnail_da_live("https://x/capa.jpg", 1280, 720) == "https://x/capa.jpg"


def test_previa_vazia_vira_nada():
    assert thumbnail_da_live("", 1280, 720) is None


def test_live_sem_jogo_nao_deixa_separador_solto():
    item = live_para_item(live(game_name="", viewer_count=0), login="x")

    assert item.detalhe == ""


def test_live_sem_titulo_ainda_rende_um_item_usavel():
    item = live_para_item(live(title=""), login="x")

    assert item.titulo == "Live"


# ---------------------------------------------------------------------------
# Twitch: um aviso por live
# ---------------------------------------------------------------------------


async def assinar_twitch(database, **opcoes):
    assinatura = await criar_assinatura(
        database,
        guild_id=GUILD,
        tipo=TIPO_TWITCH,
        externo_id="123456",
        nome="Gaules",
        url="https://www.twitch.tv/gaules",
        channel_id=CANAL,
        cargo_id=None,
        opcoes={"login": "gaules", **opcoes},
    )
    assert assinatura is not None
    return assinatura


async def test_canal_offline_no_cadastro_e_avisa_quando_abrir(db):
    assinatura = await assinar_twitch(db)
    cog = CogDeTeste(db, [])  # offline
    await cog.primeira_carga(assinatura)

    cog.itens = [live_para_item(live("111"), login="gaules")]
    await cog.processar(await get_assinatura(db, assinatura.id))

    assert [i.id for i in cog.avisados[0]] == ["live:111"]


async def test_a_mesma_live_nao_avisa_duas_vezes(db):
    """A checagem roda de 2 em 2 minutos com a live no ar o tempo todo."""
    assinatura = await assinar_twitch(db)
    cog = CogDeTeste(db, [live_para_item(live("111"), login="gaules")])
    await cog.processar(assinatura)

    await cog.processar(await get_assinatura(db, assinatura.id))
    await cog.processar(await get_assinatura(db, assinatura.id))

    assert len(cog.avisados) == 1, "uma live, um aviso"


async def test_live_nova_depois_de_fechar_avisa_de_novo(db):
    assinatura = await assinar_twitch(db)
    cog = CogDeTeste(db, [live_para_item(live("111"), login="gaules")])
    await cog.processar(assinatura)

    cog.itens = []  # fechou a live
    await cog.processar(await get_assinatura(db, assinatura.id))

    cog.itens = [live_para_item(live("222"), login="gaules")]  # abriu outra
    await cog.processar(await get_assinatura(db, assinatura.id))

    assert [i.id for lote in cog.avisados for i in lote] == ["live:111", "live:222"]


async def test_quem_ja_estava_ao_vivo_no_cadastro_nao_gera_aviso(db):
    """Mesma regra das outras fontes: assinar não dispara alerta retroativo."""
    assinatura = await assinar_twitch(db)
    cog = CogDeTeste(db, [live_para_item(live("111"), login="gaules")])

    await cog.primeira_carga(assinatura)

    assert cog.avisados == []
    assert await ids_vistos(db, assinatura.id) == {"live:111"}


# ---------------------------------------------------------------------------
# Menções
# ---------------------------------------------------------------------------


def assinatura_com(**campos):
    base = dict(
        id=1,
        guild_id=GUILD,
        tipo=TIPO_TWITCH,
        externo_id="123",
        nome="Gaules",
        url="",
        channel_id=CANAL,
        cargo_id=None,
        opcoes={},
    )
    base.update(campos)
    return Assinatura(**base)


def _mencoes(assinatura):
    return Alertas.mencoes_da(Alertas.__new__(Alertas), assinatura)


def test_everyone_pinga_todo_mundo():
    conteudo, permitidas = _mencoes(assinatura_com(opcoes={"everyone": True}))

    assert conteudo == "@everyone"
    assert permitidas.to_dict() == {"parse": ["everyone"]}


def test_everyone_e_cargo_convivem():
    conteudo, permitidas = _mencoes(
        assinatura_com(opcoes={"everyone": True}, cargo_id=777)
    )

    assert conteudo == "@everyone <@&777>"
    assert permitidas.to_dict() == {"roles": [777], "parse": ["everyone"]}


def test_so_cargo_nao_libera_everyone():
    """Regressão: liberar everyone sem pedir furaria a política global do bot."""
    _, permitidas = _mencoes(assinatura_com(cargo_id=777))

    assert permitidas.to_dict() == {"roles": [777], "parse": []}


def test_sem_mencao_nenhuma_o_conteudo_fica_vazio():
    conteudo, permitidas = _mencoes(assinatura_com())

    assert conteudo is None
    assert permitidas.to_dict() == {"parse": []}


def test_usuarios_nunca_entram_na_liberacao():
    """Nenhuma combinação pode acabar permitindo ping de usuário."""
    for opcoes, cargo in (({}, None), ({"everyone": True}, None), ({}, 1), ({"everyone": True}, 1)):
        _, permitidas = _mencoes(assinatura_com(opcoes=opcoes, cargo_id=cargo))
        assert "users" not in permitidas.to_dict().get("parse", [])


# ---------------------------------------------------------------------------
# Twitch: token e chamadas à Helix
#
# O caminho feliz não dá para exercitar contra a Twitch de verdade sem uma
# credencial válida, então a sessão HTTP é dublê. O que está coberto é o que é
# nosso: cache do token, renovação no 401 e tradução das respostas.
# ---------------------------------------------------------------------------


class RespostaFalsa:
    def __init__(self, status: int, payload) -> None:
        self.status = status
        self._payload = payload

    async def json(self, content_type=None):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False


class SessaoFalsa:
    """Devolve respostas roteirizadas e guarda o que foi pedido."""

    def __init__(self, tokens=None, gets=None) -> None:
        self.tokens = list(tokens or [])
        self.gets = list(gets or [])
        self.posts_feitos: list[dict] = []
        self.gets_feitos: list[tuple] = []
        self.closed = False

    def post(self, url, data=None, timeout=None):
        self.posts_feitos.append(dict(data or {}))
        return self.tokens.pop(0)

    def get(self, url, params=None, headers=None, timeout=None):
        self.gets_feitos.append((url, params, dict(headers or {})))
        return self.gets.pop(0)


def cog_twitch(sessao, *, configurada: bool = True):
    from types import SimpleNamespace

    cog = Alertas.__new__(Alertas)
    cog.bot = SimpleNamespace(
        http_session=sessao,
        config=SimpleNamespace(
            twitch_client_id="cid" if configurada else None,
            twitch_client_secret="segredo" if configurada else None,
            twitch_configurada=configurada,
        ),
    )
    cog._trava_twitch = asyncio.Lock()
    cog._token_twitch = None
    cog._token_twitch_expira = datetime.min.replace(tzinfo=timezone.utc)
    return cog


def token_ok(segundos: int = 5_000_000, valor: str = "tok-123"):
    return RespostaFalsa(200, {"access_token": valor, "expires_in": segundos})


async def test_token_vai_no_corpo_como_a_twitch_pede():
    """client_id/secret/grant_type vão no corpo do POST, não na query."""
    sessao = SessaoFalsa(tokens=[token_ok()])
    cog = cog_twitch(sessao)

    assert await cog.token_twitch() == "tok-123"
    assert sessao.posts_feitos[0] == {
        "client_id": "cid",
        "client_secret": "segredo",
        "grant_type": "client_credentials",
    }


async def test_token_fica_em_cache_entre_chamadas():
    """Ele vale semanas; pedir um por checagem seria desperdício."""
    sessao = SessaoFalsa(tokens=[token_ok()])
    cog = cog_twitch(sessao)

    await cog.token_twitch()
    await cog.token_twitch()

    assert len(sessao.posts_feitos) == 1


async def test_token_perto_de_vencer_e_renovado():
    """A margem existe para a checagem não cair no instante da virada."""
    sessao = SessaoFalsa(tokens=[token_ok(segundos=60), token_ok(valor="tok-novo")])
    cog = cog_twitch(sessao)

    await cog.token_twitch()
    assert await cog.token_twitch() == "tok-novo"


async def test_credencial_recusada_vira_mensagem_util():
    sessao = SessaoFalsa(tokens=[RespostaFalsa(400, {"message": "invalid client"})])
    cog = cog_twitch(sessao)

    with pytest.raises(FonteIndisponivel) as erro:
        await cog.token_twitch()

    assert "TWITCH_CLIENT_ID" in str(erro.value)


async def test_sem_credencial_a_mensagem_ensina_o_caminho():
    cog = cog_twitch(SessaoFalsa(), configurada=False)

    with pytest.raises(FonteIndisponivel) as erro:
        await cog.token_twitch()

    assert "dev.twitch.tv" in str(erro.value)


async def test_chamada_manda_client_id_e_bearer():
    sessao = SessaoFalsa(tokens=[token_ok()], gets=[RespostaFalsa(200, {"data": []})])
    cog = cog_twitch(sessao)

    await cog.twitch("/streams", [("user_id", "1")])

    _, _, cabecalhos = sessao.gets_feitos[0]
    assert cabecalhos["Client-Id"] == "cid"
    assert cabecalhos["Authorization"] == "Bearer tok-123"


async def test_token_revogado_e_renovado_uma_vez():
    """401 no meio do caminho não pode virar falha da assinatura."""
    sessao = SessaoFalsa(
        tokens=[token_ok(), token_ok(valor="tok-novo")],
        gets=[RespostaFalsa(401, {}), RespostaFalsa(200, {"data": [live("7")]})],
    )
    cog = cog_twitch(sessao)

    dados = await cog.twitch("/streams", [("user_id", "1")])

    assert dados["data"][0]["id"] == "7"
    assert len(sessao.posts_feitos) == 2, "o token foi renovado"
    assert sessao.gets_feitos[1][2]["Authorization"] == "Bearer tok-novo"


async def test_401_duas_vezes_desiste_sem_laco_infinito():
    sessao = SessaoFalsa(
        tokens=[token_ok(), token_ok()],
        gets=[RespostaFalsa(401, {}), RespostaFalsa(401, {})],
    )
    cog = cog_twitch(sessao)

    with pytest.raises(FonteIndisponivel):
        await cog.twitch("/streams", [("user_id", "1")])


async def test_rate_limit_da_twitch_e_traduzido():
    sessao = SessaoFalsa(tokens=[token_ok()], gets=[RespostaFalsa(429, {})])
    cog = cog_twitch(sessao)

    with pytest.raises(FonteIndisponivel) as erro:
        await cog.twitch("/streams", [("user_id", "1")])

    assert "limitando" in str(erro.value)


async def test_coletar_traduz_a_live_em_item():
    sessao = SessaoFalsa(
        tokens=[token_ok()], gets=[RespostaFalsa(200, {"data": [live("55")]})]
    )
    cog = cog_twitch(sessao)
    assinatura = assinatura_com(externo_id="123", opcoes={"login": "gaules"})

    itens = await cog.coletar(assinatura)

    assert [i.id for i in itens] == ["live:55"]
    assert sessao.gets_feitos[0][1] == [("user_id", "123")]


async def test_coletar_de_canal_offline_nao_rende_item():
    """Offline é lista vazia na Helix; nada a avisar, e nenhuma falha."""
    sessao = SessaoFalsa(tokens=[token_ok()], gets=[RespostaFalsa(200, {"data": []})])
    cog = cog_twitch(sessao)

    assert await cog.coletar(assinatura_com(opcoes={"login": "x"})) == []


async def test_resolver_canal_guarda_o_id_e_nao_o_login():
    """Quem troca o nome do canal na Twitch continua sendo o mesmo id."""
    sessao = SessaoFalsa(
        tokens=[token_ok()],
        gets=[
            RespostaFalsa(
                200, {"data": [{"id": "9876", "login": "gaules", "display_name": "Gaules"}]}
            )
        ],
    )
    cog = cog_twitch(sessao)

    assert await cog.resolver_twitch("twitch.tv/Gaules") == ("9876", "Gaules", "gaules")


async def test_canal_inexistente_avisa_em_vez_de_cadastrar_vazio():
    sessao = SessaoFalsa(tokens=[token_ok()], gets=[RespostaFalsa(200, {"data": []})])
    cog = cog_twitch(sessao)

    with pytest.raises(FonteIndisponivel) as erro:
        await cog.resolver_twitch("ninguem_aqui")

    assert "ninguem_aqui" in str(erro.value)
