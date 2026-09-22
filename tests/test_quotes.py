"""Testes da segmentação de emoji e da medição do card de citação.

Nada aqui baixa nada: as funções são puras e recebem os sprites já resolvidos. O
foco é o que quebra em silêncio — um emoji segmentado errado vira vários quadrados,
e um emoji medido errado empurra o texto para fora da margem.
"""

from __future__ import annotations

from datetime import datetime, timezone

from leviathan.cogs.quotes import (
    LARGURA,
    MARGEM,
    QUEBRA,
    _fonte,
    _largura_palavra,
    _nomes_twemoji,
    formatar_data,
    quebrar_linhas,
    refs_de,
    render_quote_card,
    segmentar,
    truncar,
)

VS = "️"
ZWJ = "‍"

FAMILIA = f"👨{ZWJ}👩{ZWJ}👧{ZWJ}👦"
JOINHA_TOM = "👍🏽"


# ---------------------------------------------------------------------------
# Nomes de arquivo do Twemoji
# ---------------------------------------------------------------------------


def test_nome_twemoji_tenta_sem_fe0f_primeiro():
    # Confirmado contra o CDN: 2764.png existe, 2764-fe0f.png dá 404.
    assert _nomes_twemoji(f"❤{VS}") == ("2764", "2764-fe0f")


def test_nome_twemoji_sem_variation_selector():
    assert _nomes_twemoji("😀") == ("1f600",)


def test_nome_twemoji_de_sequencia_zwj():
    assert _nomes_twemoji(f"👩{ZWJ}💻") == ("1f469-200d-1f4bb",)


def test_nome_twemoji_com_tom_de_pele():
    assert _nomes_twemoji(JOINHA_TOM) == ("1f44d-1f3fd",)


def test_nome_twemoji_de_bandeira():
    assert _nomes_twemoji("🇧🇷") == ("1f1e7-1f1f7",)


# ---------------------------------------------------------------------------
# Segmentação
# ---------------------------------------------------------------------------


def test_texto_sem_emoji_vira_so_palavras():
    palavras = segmentar("oi tudo bem")

    assert len(palavras) == 3
    assert all(atomo.emoji is None for palavra in palavras for atomo in palavra)


def test_sequencia_zwj_e_um_emoji_so():
    """Se o ZWJ fosse ignorado, a família viraria quatro emoji separados."""
    refs = refs_de(f"olha a {FAMILIA} ali")

    assert len(refs) == 1
    assert next(iter(refs)) == "u-1f468-200d-1f469-200d-1f467-200d-1f466"


def test_tom_de_pele_fica_junto_do_emoji_base():
    refs = refs_de(f"boa {JOINHA_TOM}")

    assert list(refs) == ["u-1f44d-1f3fd"]


def test_emoji_custom_do_discord():
    refs = refs_de("olha <:leviathan:1234567890123456789> aqui")

    assert list(refs) == ["c-1234567890123456789"]
    ref = refs["c-1234567890123456789"]
    assert ref.fallback == ":leviathan:"
    assert ref.urls == ("https://cdn.discordapp.com/emojis/1234567890123456789.png",)


def test_emoji_custom_animado_tambem_e_reconhecido():
    assert list(refs_de("<a:dancando:1234567890123456789>")) == ["c-1234567890123456789"]


def test_emoji_grudado_no_texto_nao_ganha_espaco():
    palavras = segmentar("oi😀")

    assert len(palavras) == 1, "o emoji faz parte da mesma palavra"
    assert palavras[0][0].texto == "oi"
    assert palavras[0][1].emoji is not None


def test_quebra_de_linha_vira_sentinela():
    palavras = segmentar("uma\noutra")

    assert palavras[1] is QUEBRA


def test_fallback_de_emoji_unicode_e_ascii():
    """O fallback não pode ser o próprio emoji, senão voltaria a virar quadrado."""
    for ref in refs_de(f"{FAMILIA} 😀 ☕").values():
        assert ref.fallback.isascii()
        assert ref.fallback.startswith(":")


# ---------------------------------------------------------------------------
# Medição: é o que impede o texto de vazar a margem
# ---------------------------------------------------------------------------


def test_emoji_com_sprite_mede_o_tamanho_da_fonte():
    fonte = _fonte(40, "Medium")
    palavra = segmentar("😀")[0]

    com_sprite = _largura_palavra(palavra, fonte, 40, {"u-1f600": object()})
    assert com_sprite == 40


def test_emoji_sem_sprite_mede_o_texto_do_fallback():
    fonte = _fonte(40, "Medium")
    palavra = segmentar("😀")[0]

    sem_sprite = _largura_palavra(palavra, fonte, 40, {})
    assert sem_sprite == fonte.getlength(":grinning_face:")


def test_linha_com_emoji_respeita_a_largura_maxima():
    fonte = _fonte(40, "Medium")
    sprites = {chave: object() for chave in refs_de("😀")}
    texto = "palavra 😀 " * 20
    largura_max = 800

    linhas = quebrar_linhas(segmentar(texto), fonte, 40, largura_max, sprites)

    espaco = fonte.getlength(" ")
    for linha in linhas:
        largura = sum(_largura_palavra(p, fonte, 40, sprites) for p in linha)
        largura += espaco * max(len(linha) - 1, 0)
        assert largura <= largura_max


# ---------------------------------------------------------------------------
# Card completo
# ---------------------------------------------------------------------------


def test_render_com_emoji_sem_sprite_nao_quebra():
    png = render_quote_card(
        texto=f"deu ruim 😀 {FAMILIA}",
        autor="João",
        data=datetime(2026, 9, 14, 21, 30, tzinfo=timezone.utc),
        canal="geral",
    )
    assert png.startswith(b"\x89PNG")


def test_data_usa_fuso_de_brasilia():
    # 00:30 UTC do dia 15 ainda é dia 14 em Brasília.
    assert formatar_data(datetime(2026, 9, 15, 0, 30, tzinfo=timezone.utc)) == (
        "14 de setembro de 2026"
    )


def test_truncamento_no_limite():
    assert len(truncar("a" * 400)) == 400
    assert truncar("a" * 401).endswith("…")


def test_largura_util_do_texto_bate_com_o_layout():
    # Guarda contra alguém mexer no layout e esquecer da margem direita.
    assert LARGURA - MARGEM > MARGEM
