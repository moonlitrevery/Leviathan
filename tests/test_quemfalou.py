"""Testes da lógica pura do jogo "quem falou?".

Fora do alcance daqui: a leitura do histórico e a publicação da rodada, que
dependem do Discord. O que está coberto é o que decide sozinho — quais mensagens
podem virar rodada, quais canais e citações são sorteáveis, quando o bot fica
calado, a regra de um palpite por pessoa e quando o agendador entende que o jogo
ainda tem gente.

Os canais e a guild aqui são dublês: ``canais_sorteaveis`` e ``escolher_citacao``
só leem permissões e ids, então dá para exercitá-los sem subir nada do Discord.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from leviathan.cogs.quemfalou import (
    MIN_CARACTERES,
    MIN_PALAVRAS,
    QuemFalou,
    contar_palpites,
    criar_rodada,
    data_aproximada,
    dentro_do_silencio,
    escolher_citacao,
    houve_movimento,
    houve_palpite_na_ultima_rodada,
    marcar_acerto,
    mensagem_elegivel,
    registrar_palpite,
    texto_elegivel,
)
from leviathan.db import Database, init_db

GUILD = 111
CANAL_JOGO = 999
CANAL_QUALQUER = 222
AUTOR = 333
ANA = 1001
BRUNO = 1002

CANAL_PUBLICO = 201
CANAL_PRIVADO = 202
CANAL_EXCLUIDO = 203
CANAL_SUMIDO = 204

FRASE = "essa frase tem tamanho suficiente para virar rodada"


@pytest.fixture
async def db(tmp_path):
    database = Database(tmp_path / "teste.db", pool_size=2)
    await database.connect()
    await init_db(database)
    yield database
    await database.close()


@pytest.fixture
async def rodada(db):
    criada = await criar_rodada(
        db,
        guild_id=GUILD,
        game_channel_id=CANAL_JOGO,
        origem_channel_id=CANAL_QUALQUER,
        origem_message_id=555,
        autor_id=AUTOR,
        conteudo=FRASE,
        origem_criada_em=datetime(2025, 3, 10, tzinfo=timezone.utc),
        jump_url="https://discord.com/x",
        expira_em=datetime.now(timezone.utc) + timedelta(minutes=10),
    )
    assert criada is not None
    return criada


# ---------------------------------------------------------------------------
# Filtro da mensagem
# ---------------------------------------------------------------------------


def test_frase_normal_e_elegivel():
    assert texto_elegivel(FRASE) is True


def test_curta_demais_nao_entra():
    curta = "opa beleza cara"
    assert len(curta) < MIN_CARACTERES
    assert texto_elegivel(curta) is False


def test_poucas_palavras_nao_entra():
    # Passa nos 25 caracteres, mas não nas 4 palavras.
    tres_palavras = "supercalifragilisticexpialidocious antidisestablishmentarianism ok"
    assert len(tres_palavras) >= MIN_CARACTERES
    assert len(tres_palavras.split()) < MIN_PALAVRAS
    assert texto_elegivel(tres_palavras) is False


def test_so_link_nao_entra():
    assert texto_elegivel("https://exemplo.com/um/caminho/bem/longo/mesmo") is False
    assert texto_elegivel("https://a.com https://b.com https://c.com") is False


def test_link_com_texto_em_volta_entra():
    assert texto_elegivel("olha o que eu achei nesse site aqui https://exemplo.com") is True


def test_texto_que_sobra_pouco_depois_do_link_nao_entra():
    """O link não pode ser o que faz a mensagem parecer longa."""
    assert texto_elegivel("olha https://exemplo.com/um/caminho/bem/longo/mesmo") is False


@pytest.mark.parametrize(
    "comando",
    ["!play alguma musica ai", "/ping teste de comando", ".help me com isso aqui",
     "?fm tocando agora mesmo", "--flag valor outro valor"],
)
def test_comando_de_bot_nao_entra(comando):
    assert texto_elegivel(comando) is False


def test_pontuacao_no_meio_da_frase_nao_e_comando():
    assert texto_elegivel("nossa... isso aí foi muito estranho mesmo") is True


def test_mensagem_de_bot_nao_entra():
    assert (
        mensagem_elegivel(
            conteudo=FRASE,
            autor_e_bot=True,
            autor_no_servidor=True,
            channel_id=CANAL_QUALQUER,
            canal_do_jogo=CANAL_JOGO,
        )
        is False
    )


def test_autor_que_saiu_do_servidor_nao_entra():
    """Se o autor saiu, ninguém consegue acertar e a rodada é desperdiçada."""
    assert (
        mensagem_elegivel(
            conteudo=FRASE,
            autor_e_bot=False,
            autor_no_servidor=False,
            channel_id=CANAL_QUALQUER,
            canal_do_jogo=CANAL_JOGO,
        )
        is False
    )


def test_mensagem_do_proprio_canal_do_jogo_nao_entra():
    assert (
        mensagem_elegivel(
            conteudo=FRASE,
            autor_e_bot=False,
            autor_no_servidor=True,
            channel_id=CANAL_JOGO,
            canal_do_jogo=CANAL_JOGO,
        )
        is False
    )


def test_mensagem_valida_passa_por_todos_os_filtros():
    assert (
        mensagem_elegivel(
            conteudo=FRASE,
            autor_e_bot=False,
            autor_no_servidor=True,
            channel_id=CANAL_QUALQUER,
            canal_do_jogo=CANAL_JOGO,
        )
        is True
    )


# ---------------------------------------------------------------------------
# Horário de silêncio
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("hora", [2, 3, 7, 8])
def test_silencio_padrao_cala_de_madrugada(hora):
    assert dentro_do_silencio(hora, 2, 9) is True


@pytest.mark.parametrize("hora", [0, 1, 9, 12, 23])
def test_silencio_padrao_libera_o_resto(hora):
    assert dentro_do_silencio(hora, 2, 9) is False


def test_limites_da_janela_sao_meio_abertos():
    # Começa em 2 (inclusivo) e termina em 9 (exclusivo).
    assert dentro_do_silencio(2, 2, 9) is True
    assert dentro_do_silencio(9, 2, 9) is False


@pytest.mark.parametrize("hora", [22, 23, 0, 1, 5])
def test_janela_que_vira_a_meia_noite_cala(hora):
    assert dentro_do_silencio(hora, 22, 6) is True


@pytest.mark.parametrize("hora", [6, 7, 12, 21])
def test_janela_que_vira_a_meia_noite_libera(hora):
    assert dentro_do_silencio(hora, 22, 6) is False


def test_janela_vazia_nunca_cala():
    for hora in range(24):
        assert dentro_do_silencio(hora, 3, 3) is False


def test_janela_de_uma_hora():
    assert dentro_do_silencio(3, 3, 4) is True
    assert dentro_do_silencio(4, 3, 4) is False


def test_data_aproximada_nao_entrega_o_dia():
    quando = datetime(2025, 3, 10, 15, 30, tzinfo=timezone.utc)
    assert data_aproximada(quando) == "março de 2025"


# ---------------------------------------------------------------------------
# Um palpite por pessoa
# ---------------------------------------------------------------------------


async def test_primeiro_palpite_e_aceito(db, rodada):
    assert await registrar_palpite(db, rodada.id, ANA, AUTOR, True) is True


async def test_segundo_palpite_da_mesma_pessoa_e_recusado(db, rodada):
    await registrar_palpite(db, rodada.id, ANA, BRUNO, False)

    assert await registrar_palpite(db, rodada.id, ANA, AUTOR, True) is False, (
        "errar e tentar de novo não pode valer"
    )
    total, errados = await contar_palpites(db, rodada.id)
    assert (total, errados) == (1, 1), "o palpite recusado não entra na contagem"


async def test_pessoas_diferentes_palpitam_na_mesma_rodada(db, rodada):
    assert await registrar_palpite(db, rodada.id, ANA, BRUNO, False) is True
    assert await registrar_palpite(db, rodada.id, BRUNO, AUTOR, True) is True

    total, errados = await contar_palpites(db, rodada.id)
    assert (total, errados) == (2, 1)


async def test_mesma_pessoa_palpita_em_rodadas_diferentes(db, rodada):
    outra = await criar_rodada(
        db,
        guild_id=GUILD,
        game_channel_id=CANAL_JOGO,
        origem_channel_id=CANAL_QUALQUER,
        origem_message_id=556,
        autor_id=AUTOR,
        conteudo=FRASE,
        origem_criada_em=datetime(2025, 4, 1, tzinfo=timezone.utc),
        jump_url="https://discord.com/y",
        expira_em=datetime.now(timezone.utc) + timedelta(minutes=10),
    )
    assert outra is not None

    assert await registrar_palpite(db, rodada.id, ANA, BRUNO, False) is True
    assert await registrar_palpite(db, outra.id, ANA, AUTOR, True) is True


async def test_mensagem_ja_usada_nao_vira_rodada_de_novo(db, rodada):
    repetida = await criar_rodada(
        db,
        guild_id=GUILD,
        game_channel_id=CANAL_JOGO,
        origem_channel_id=CANAL_QUALQUER,
        origem_message_id=rodada.origem_message_id,
        autor_id=AUTOR,
        conteudo=FRASE,
        origem_criada_em=datetime(2025, 3, 10, tzinfo=timezone.utc),
        jump_url="https://discord.com/x",
        expira_em=datetime.now(timezone.utc) + timedelta(minutes=10),
    )
    assert repetida is None


async def test_so_um_acerto_encerra_a_rodada(db, rodada):
    """Dois acertos simultâneos disputam o mesmo UPDATE condicional."""
    assert await marcar_acerto(db, rodada.id, ANA) is True
    assert await marcar_acerto(db, rodada.id, BRUNO) is False, "a rodada já não está ativa"


# ---------------------------------------------------------------------------
# Privacidade: canais sorteáveis e fonte "citações"
# ---------------------------------------------------------------------------

EVERYONE = object()
EU = object()


class FakePermissoes:
    def __init__(self, pode: bool) -> None:
        self.view_channel = pode
        self.read_message_history = pode


class FakeCanal:
    """Canal com permissões fixas para o @everyone e para o bot."""

    def __init__(self, id: int, *, publico: bool = True, bot_le: bool = True) -> None:
        self.id = id
        self.name = f"canal-{id}"
        self._publico = publico
        self._bot_le = bot_le

    def permissions_for(self, alvo):
        return FakePermissoes(self._publico if alvo is EVERYONE else self._bot_le)


class FakeGuild:
    def __init__(self, canais) -> None:
        self.id = GUILD
        self.default_role = EVERYONE
        self.me = EU
        self.text_channels = list(canais)


def canais_permitidos(canais, *, excluidos=frozenset()) -> set[int]:
    """Os ids que o jogo aceita sortear, pela mesma função que o cog usa."""
    sorteaveis = QuemFalou.canais_sorteaveis(
        None,
        FakeGuild(canais),
        excluidos=set(excluidos),
        canal_do_jogo=CANAL_JOGO,
    )
    return {canal.id for canal in sorteaveis}


def citacao(message_id: int, channel_id: int, *, autor: int = AUTOR, texto: str = FRASE):
    return {
        "message_id": message_id,
        "channel_id": channel_id,
        "author_id": autor,
        "content": texto,
        "created_at": "2025-03-10T00:00:00+00:00",
        "jump_url": "https://discord.com/x",
    }


def escolher(citacoes, *, permitidos, usadas=frozenset(), no_servidor=(AUTOR,)):
    return escolher_citacao(
        citacoes,
        usadas=set(usadas),
        canais_permitidos=permitidos,
        autor_no_servidor=lambda autor: autor in no_servidor,
    )


def test_canal_privado_fica_fora_dos_sorteaveis():
    publico = FakeCanal(CANAL_PUBLICO)
    privado = FakeCanal(CANAL_PRIVADO, publico=False)

    assert canais_permitidos([publico, privado]) == {CANAL_PUBLICO}


def test_canal_que_o_bot_nao_le_fica_fora():
    assert canais_permitidos([FakeCanal(CANAL_PUBLICO, bot_le=False)]) == set()


def test_canal_do_jogo_fica_fora():
    assert canais_permitidos([FakeCanal(CANAL_JOGO)]) == set()


def test_citacao_de_canal_privado_nunca_e_escolhida():
    """Mesmo sendo a única citação disponível, ela não pode vazar."""
    permitidos = canais_permitidos(
        [FakeCanal(CANAL_PUBLICO), FakeCanal(CANAL_PRIVADO, publico=False)]
    )

    assert escolher([citacao(1, CANAL_PRIVADO)], permitidos=permitidos) is None


def test_citacao_de_canal_excluido_nunca_e_escolhida():
    permitidos = canais_permitidos(
        [FakeCanal(CANAL_PUBLICO), FakeCanal(CANAL_EXCLUIDO)],
        excluidos={CANAL_EXCLUIDO},
    )

    assert escolher([citacao(1, CANAL_EXCLUIDO)], permitidos=permitidos) is None


def test_citacao_de_canal_que_nao_existe_mais_e_descartada():
    permitidos = canais_permitidos([FakeCanal(CANAL_PUBLICO)])

    assert escolher([citacao(1, CANAL_SUMIDO)], permitidos=permitidos) is None


def test_citacao_de_canal_publico_e_escolhida():
    permitidos = canais_permitidos([FakeCanal(CANAL_PUBLICO)])

    escolhida = escolher([citacao(1, CANAL_PUBLICO)], permitidos=permitidos)

    assert escolhida is not None and escolhida["message_id"] == 1


def test_percorre_a_amostra_ate_achar_uma_que_passe_em_tudo():
    """Uma citação reprovada não pode fazer a rodada inteira desistir."""
    permitidos = canais_permitidos(
        [FakeCanal(CANAL_PUBLICO), FakeCanal(CANAL_PRIVADO, publico=False)]
    )
    amostra = [
        citacao(1, CANAL_PUBLICO),  # já usada
        citacao(2, CANAL_PRIVADO),  # canal restrito
        citacao(3, CANAL_SUMIDO),  # canal que sumiu
        citacao(4, CANAL_PUBLICO, autor=BRUNO),  # autor saiu do servidor
        citacao(5, CANAL_PUBLICO, texto="curta"),  # texto inelegível
        citacao(6, CANAL_PUBLICO),  # esta serve
    ]

    escolhida = escolher(amostra, permitidos=permitidos, usadas={1})

    assert escolhida is not None and escolhida["message_id"] == 6


def test_amostra_toda_reprovada_devolve_nada():
    permitidos = canais_permitidos([FakeCanal(CANAL_PUBLICO)])
    amostra = [citacao(1, CANAL_PRIVADO), citacao(2, CANAL_EXCLUIDO)]

    assert escolher(amostra, permitidos=permitidos) is None


# ---------------------------------------------------------------------------
# Movimento: palpite conta tanto quanto mensagem
# ---------------------------------------------------------------------------


class Espiao:
    """Callable assíncrono que registra se chegou a ser chamado."""

    def __init__(self, resposta: bool) -> None:
        self.resposta = resposta
        self.chamado = False

    async def __call__(self) -> bool:
        self.chamado = True
        return self.resposta


ONTEM = datetime(2025, 3, 10, tzinfo=timezone.utc)


async def test_sem_rodada_anterior_sempre_ha_movimento():
    palpite, mensagem = Espiao(False), Espiao(False)

    assert await houve_movimento(None, tem_palpite=palpite, tem_mensagem=mensagem) is True
    assert not palpite.chamado and not mensagem.chamado


async def test_palpite_conta_como_movimento():
    """Num canal dedicado, as pessoas jogam pelo select sem escrever nada."""
    palpite, mensagem = Espiao(True), Espiao(False)

    assert await houve_movimento(ONTEM, tem_palpite=palpite, tem_mensagem=mensagem) is True


async def test_historico_nao_e_lido_quando_ja_houve_palpite():
    """Consulta ao banco primeiro; a chamada à API só se precisar."""
    palpite, mensagem = Espiao(True), Espiao(True)

    await houve_movimento(ONTEM, tem_palpite=palpite, tem_mensagem=mensagem)

    assert palpite.chamado and not mensagem.chamado


async def test_sem_palpite_o_historico_decide():
    palpite, mensagem = Espiao(False), Espiao(True)

    assert await houve_movimento(ONTEM, tem_palpite=palpite, tem_mensagem=mensagem) is True
    assert mensagem.chamado


async def test_sem_palpite_e_sem_mensagem_nao_ha_movimento():
    palpite, mensagem = Espiao(False), Espiao(False)

    assert await houve_movimento(ONTEM, tem_palpite=palpite, tem_mensagem=mensagem) is False


async def test_rodada_sem_palpite_nao_conta_como_movimento(db, rodada):
    assert await houve_palpite_na_ultima_rodada(db, GUILD) is False


async def test_palpite_na_ultima_rodada_e_encontrado(db, rodada):
    await registrar_palpite(db, rodada.id, ANA, BRUNO, False)

    assert await houve_palpite_na_ultima_rodada(db, GUILD) is True


async def test_palpite_de_rodada_antiga_nao_vale(db, rodada):
    """Só a rodada mais recente diz se o jogo ainda tem gente."""
    await registrar_palpite(db, rodada.id, ANA, BRUNO, False)
    nova = await criar_rodada(
        db,
        guild_id=GUILD,
        game_channel_id=CANAL_JOGO,
        origem_channel_id=CANAL_QUALQUER,
        origem_message_id=557,
        autor_id=AUTOR,
        conteudo=FRASE,
        origem_criada_em=datetime(2025, 5, 1, tzinfo=timezone.utc),
        jump_url="https://discord.com/z",
        expira_em=datetime.now(timezone.utc) + timedelta(minutes=10),
    )
    assert nova is not None

    assert await houve_palpite_na_ultima_rodada(db, GUILD) is False


async def test_guild_sem_rodada_nenhuma_nao_tem_palpite(db):
    assert await houve_palpite_na_ultima_rodada(db, 42) is False
