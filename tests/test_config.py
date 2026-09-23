"""Testes da leitura do ``.env``.

O foco é o ``GUILD_ID``, que aceita um servidor só — como sempre aceitou — ou
vários separados por vírgula. Um erro aqui aparece no boot, antes de qualquer
log útil, então a mensagem precisa dizer exatamente qual entrada está errada.
"""

from __future__ import annotations

import pytest

from leviathan.config import Config, ConfigError, parse_guild_ids

TOKEN = "um.token.qualquer"


@pytest.fixture
def ambiente(monkeypatch, tmp_path):
    """Ambiente limpo: nada do .env real da máquina vaza para o teste."""
    for variavel in ("DISCORD_TOKEN", "GUILD_ID", "DATABASE_PATH", "LASTFM_API_KEY"):
        monkeypatch.delenv(variavel, raising=False)
    monkeypatch.setenv("DISCORD_TOKEN", TOKEN)
    # Um .env que não existe: load_dotenv não faz nada e o os.environ manda.
    return lambda **campos: _carregar(monkeypatch, tmp_path, **campos)


def _carregar(monkeypatch, tmp_path, **campos):
    for nome, valor in campos.items():
        monkeypatch.setenv(nome, valor)
    return Config.load(env_file=tmp_path / "inexistente.env")


# ---------------------------------------------------------------------------
# Parse da lista
# ---------------------------------------------------------------------------


def test_um_id_so_continua_funcionando():
    """O .env de quem já usava o bot não pode precisar de mudança."""
    assert parse_guild_ids("123456789") == ((123456789,), [])


def test_varios_ids_separados_por_virgula():
    assert parse_guild_ids("123,456,789") == ((123, 456, 789), [])


def test_espacos_em_volta_sao_ignorados():
    assert parse_guild_ids("  123 , 456  ") == ((123, 456), [])


def test_virgula_sobrando_no_fim_nao_e_erro():
    assert parse_guild_ids("123, 456,") == ((123, 456), [])


def test_virgulas_vazias_no_meio_sao_ignoradas():
    assert parse_guild_ids("123,,456") == ((123, 456), [])


def test_ordem_do_env_e_preservada():
    ids, _ = parse_guild_ids("999, 111, 555")

    assert ids == (999, 111, 555)


def test_id_repetido_entra_uma_vez_so():
    """Listar o mesmo servidor duas vezes sincronizaria duas vezes à toa."""
    assert parse_guild_ids("123, 456, 123") == ((123, 456), [])


def test_so_virgulas_nao_rende_id_nenhum():
    assert parse_guild_ids(",,,") == ((), [])


# ---------------------------------------------------------------------------
# Entradas inválidas
# ---------------------------------------------------------------------------


def test_texto_no_lugar_do_id_vira_problema():
    ids, problemas = parse_guild_ids("abc")

    assert ids == ()
    assert len(problemas) == 1
    assert "'abc'" in problemas[0], "a mensagem precisa apontar o valor problemático"


def test_id_zero_ou_negativo_e_recusado():
    _, problemas = parse_guild_ids("0")
    assert "0" in problemas[0]

    _, problemas = parse_guild_ids("-5")
    assert "-5" in problemas[0]


def test_entrada_ruim_nao_descarta_as_boas():
    """Cada entrada é avaliada sozinha, para a mensagem listar tudo de uma vez."""
    ids, problemas = parse_guild_ids("123, abc, 456, -7")

    assert ids == (123, 456)
    assert len(problemas) == 2
    assert "'abc'" in problemas[0]
    assert "-7" in problemas[1]


def test_id_decimal_nao_passa():
    _, problemas = parse_guild_ids("123.456")

    assert "'123.456'" in problemas[0]


# ---------------------------------------------------------------------------
# Config.load
# ---------------------------------------------------------------------------


def test_config_com_um_servidor(ambiente):
    config = ambiente(GUILD_ID="123456789")

    assert config.guild_ids == (123456789,)


def test_config_com_varios_servidores(ambiente):
    config = ambiente(GUILD_ID="123, 456")

    assert config.guild_ids == (123, 456)


def test_id_invalido_vira_config_error_citando_o_valor(ambiente):
    with pytest.raises(ConfigError) as erro:
        ambiente(GUILD_ID="123, lixo")

    assert "lixo" in str(erro.value)


def test_guild_id_vazio_explica_o_que_fazer(ambiente):
    with pytest.raises(ConfigError) as erro:
        ambiente(GUILD_ID="   ")

    mensagem = str(erro.value)
    assert "GUILD_ID" in mensagem
    assert "vírgula" in mensagem, "a mensagem precisa mencionar o formato de lista"


def test_guild_id_so_com_virgulas_nao_passa_calado(ambiente):
    """Sem isso, o bot subiria sem sincronizar em servidor nenhum."""
    with pytest.raises(ConfigError) as erro:
        ambiente(GUILD_ID=",,")

    assert "GUILD_ID" in str(erro.value)


def test_todos_os_problemas_saem_juntos(ambiente, monkeypatch):
    monkeypatch.delenv("DISCORD_TOKEN", raising=False)
    monkeypatch.setenv("DISCORD_TOKEN", "")

    with pytest.raises(ConfigError) as erro:
        ambiente(GUILD_ID="abc")

    mensagem = str(erro.value)
    assert "DISCORD_TOKEN" in mensagem and "GUILD_ID" in mensagem
