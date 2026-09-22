"""Testes do cálculo de compatibilidade musical.

É a única parte do cog que faz uma conta própria — o resto é tradução da resposta
da API. A conta é uma interseção de histogramas: cada artista vira uma fatia do
total de plays da pessoa, e a compatibilidade é a soma das menores fatias.
"""

from __future__ import annotations

from leviathan.cogs.lastfm import calcular_compatibilidade


def test_listas_iguais_dao_cem_por_cento():
    top = [("Radiohead", 300), ("Björk", 200), ("Portishead", 100)]

    assert calcular_compatibilidade(top, top).porcentagem == 100.0


def test_sem_artista_em_comum_da_zero():
    a = [("Radiohead", 300), ("Björk", 200)]
    b = [("Metallica", 300), ("Slayer", 200)]

    resultado = calcular_compatibilidade(a, b)

    assert resultado.porcentagem == 0.0
    assert resultado.comuns == ()
    assert resultado.total_comuns == 0


def test_listas_vazias_nao_explodem():
    assert calcular_compatibilidade([], []).porcentagem == 0.0
    assert calcular_compatibilidade([("Radiohead", 10)], []).porcentagem == 0.0
    assert calcular_compatibilidade([], [("Radiohead", 10)]).porcentagem == 0.0


def test_sobreposicao_parcial():
    # Metade dos plays de cada um está em Radiohead.
    a = [("Radiohead", 100), ("Björk", 100)]
    b = [("Radiohead", 50), ("Metallica", 50)]

    resultado = calcular_compatibilidade(a, b)

    assert resultado.porcentagem == 50.0
    assert resultado.total_comuns == 1


def test_conta_usa_fatia_e_nao_plays_absolutos():
    """Quem ouve muito e quem ouve pouco precisam poder dar 100%."""
    ouvinte_pesado = [("Radiohead", 5000), ("Björk", 5000)]
    ouvinte_leve = [("Radiohead", 5), ("Björk", 5)]

    assert calcular_compatibilidade(ouvinte_pesado, ouvinte_leve).porcentagem == 100.0


def test_a_menor_fatia_e_que_manda():
    # A: 90% Radiohead. B: 10% Radiohead. A sobreposição é limitada pelos 10%.
    a = [("Radiohead", 90), ("Björk", 10)]
    b = [("Radiohead", 10), ("Metallica", 90)]

    assert calcular_compatibilidade(a, b).porcentagem == 10.0


def test_comparacao_ignora_caixa_e_espacos_extras():
    a = [("Radiohead", 100)]
    b = [("  radiohead ", 100)]

    resultado = calcular_compatibilidade(a, b)

    assert resultado.porcentagem == 100.0
    assert resultado.total_comuns == 1


def test_nome_exibido_vem_de_quem_ouviu_mais():
    a = [("RADIOHEAD", 10)]
    b = [("Radiohead", 500)]

    assert calcular_compatibilidade(a, b).comuns[0].nome == "Radiohead"


def test_duplicatas_sao_somadas():
    a = [("Radiohead", 60), ("radiohead", 40)]
    b = [("Radiohead", 100)]

    resultado = calcular_compatibilidade(a, b)

    assert resultado.total_comuns == 1
    assert resultado.comuns[0].plays_a == 100


def test_artistas_sem_play_sao_ignorados():
    a = [("Radiohead", 100), ("Fantasma", 0)]
    b = [("Radiohead", 100)]

    resultado = calcular_compatibilidade(a, b)

    assert resultado.porcentagem == 100.0
    assert [artista.nome for artista in resultado.comuns] == ["Radiohead"]


def test_comuns_ordenados_por_mais_ouvidos():
    a = [("Pouco", 10), ("Muito", 200), ("Medio", 50)]
    b = [("Pouco", 10), ("Muito", 200), ("Medio", 50)]

    nomes = [artista.nome for artista in calcular_compatibilidade(a, b).comuns]

    assert nomes == ["Muito", "Medio", "Pouco"]


def test_destaque_limita_a_lista_mas_nao_o_total():
    a = [(f"Artista {i}", 100 - i) for i in range(10)]

    resultado = calcular_compatibilidade(a, a, destaque=3)

    assert len(resultado.comuns) == 3
    assert resultado.total_comuns == 10


def test_guarda_os_plays_dos_dois_lados():
    a = [("Radiohead", 300)]
    b = [("Radiohead", 7)]

    comum = calcular_compatibilidade(a, b).comuns[0]

    assert (comum.plays_a, comum.plays_b) == (300, 7)
    assert comum.total == 307
