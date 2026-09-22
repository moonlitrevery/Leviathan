"""Testes da camada de dados e do estado em memória do cog de piadas.

Quase nada aqui sobe o bot: as funções testadas recebem um
:class:`~leviathan.db.Database` em cima de um SQLite temporário, então a suíte roda
em menos de um segundo. O foco é a deduplicação dos contadores, a normalização de
emoji e o casamento dos gatilhos — as partes onde um erro passa despercebido em
produção porque a reação simplesmente não conta, sem erro nenhum no log.
"""

from __future__ import annotations

import asyncio
import time

import discord
import pytest

from leviathan.cogs import piadas
from leviathan.cogs.piadas import (
    Piadas,
    compile_trigger,
    counter_ranking,
    counter_total,
    create_counter,
    delete_counter,
    emoji_key,
    find_match,
    get_counter_by_emoji,
    get_counter_by_name,
    list_triggers,
    normalize_unicode_emoji,
    parse_emoji,
    record_hit,
    remove_trigger,
    seed_guild_triggers,
    upsert_trigger,
)
from leviathan.db import MIGRATIONS, Database, init_db

GUILD = 111
OUTRA_GUILD = 222
CANAL = 333
MENSAGEM = 444
OUTRA_MENSAGEM = 445

ANA = 1001
BRUNO = 1002
CARLA = 1003
DINA = 1004

VS = "️"  # variation selector
ZWJ = "‍"  # zero width joiner


@pytest.fixture
async def db(tmp_path):
    database = Database(tmp_path / "teste.db", pool_size=2)
    await database.connect()
    await init_db(database)
    yield database
    await database.close()


@pytest.fixture
async def contador(db):
    """Um contador de 🪱 pronto para receber reações."""
    emoji = parse_emoji("🪱")
    assert emoji is not None
    return await create_counter(db, GUILD, "o Bruno pisou na minhoca", emoji, CANAL)


class _BotFake:
    """O mínimo que o cog usa fora dos comandos: banco, guilds e get_channel."""

    def __init__(self, database: Database) -> None:
        self.db = database
        self.user = None
        self.guilds: list[object] = []

    def is_ready(self) -> bool:
        return False

    def get_channel(self, _channel_id: int) -> None:
        return None


# ---------------------------------------------------------------------------
# Deduplicação: o coração do contador
# ---------------------------------------------------------------------------


async def test_mesma_reacao_conta_uma_vez_so(db, contador):
    primeira = await record_hit(db, contador.id, MENSAGEM, ANA, BRUNO)
    segunda = await record_hit(db, contador.id, MENSAGEM, ANA, BRUNO)

    assert primeira is True, "a primeira reação precisa contar"
    assert segunda is False, "a repetição precisa ser ignorada"
    assert await counter_total(db, contador.id) == 1


async def test_remover_e_reagir_de_novo_nao_conta_de_novo(db, contador):
    """Tirar a reação e recolocar é o caminho óbvio para inflar o contador.

    Como nada é apagado de ``counter_hits`` quando a reação some, o segundo
    ``record_hit`` esbarra na mesma chave primária e vira no-op.
    """
    await record_hit(db, contador.id, MENSAGEM, ANA, BRUNO)
    # (usuário remove a reação — o bot não escuta esse evento de propósito)
    recontou = await record_hit(db, contador.id, MENSAGEM, ANA, BRUNO)

    assert recontou is False
    assert await counter_total(db, contador.id) == 1


async def test_pessoas_diferentes_na_mesma_mensagem_contam(db, contador):
    assert await record_hit(db, contador.id, MENSAGEM, ANA, BRUNO) is True
    assert await record_hit(db, contador.id, MENSAGEM, CARLA, BRUNO) is True

    assert await counter_total(db, contador.id) == 2


async def test_mesma_pessoa_em_mensagens_diferentes_conta(db, contador):
    assert await record_hit(db, contador.id, MENSAGEM, ANA, BRUNO) is True
    assert await record_hit(db, contador.id, OUTRA_MENSAGEM, ANA, BRUNO) is True

    assert await counter_total(db, contador.id) == 2


async def test_contadores_diferentes_nao_se_misturam(db, contador):
    outro_emoji = parse_emoji("🔥")
    assert outro_emoji is not None
    outro = await create_counter(db, GUILD, "pegou fogo", outro_emoji, CANAL)

    await record_hit(db, contador.id, MENSAGEM, ANA, BRUNO)
    await record_hit(db, outro.id, MENSAGEM, ANA, BRUNO)

    assert await counter_total(db, contador.id) == 1
    assert await counter_total(db, outro.id) == 1


async def test_remover_contador_apaga_o_historico(db, contador):
    await record_hit(db, contador.id, MENSAGEM, ANA, BRUNO)
    await delete_counter(db, contador.id)

    assert await counter_total(db, contador.id) == 0
    assert await get_counter_by_name(db, GUILD, contador.name) is None


# ---------------------------------------------------------------------------
# Ranking
# ---------------------------------------------------------------------------


async def test_ranking_ordena_por_quem_mais_recebeu(db, contador):
    # Bruno recebe 3, Carla 2, Dina 1.
    for mensagem, reator in ((1, ANA), (2, ANA), (3, CARLA)):
        await record_hit(db, contador.id, mensagem, reator, BRUNO)
    for mensagem in (4, 5):
        await record_hit(db, contador.id, mensagem, ANA, CARLA)
    await record_hit(db, contador.id, 6, ANA, DINA)

    assert await counter_total(db, contador.id) == 6
    assert await counter_ranking(db, contador.id, 3) == [(BRUNO, 3), (CARLA, 2), (DINA, 1)]


async def test_ranking_vazio(db, contador):
    assert await counter_ranking(db, contador.id, 3) == []


# ---------------------------------------------------------------------------
# Normalização de emoji unicode
# ---------------------------------------------------------------------------


def test_coracao_com_e_sem_variation_selector_tem_a_mesma_chave():
    assert emoji_key("❤️") == emoji_key("❤")
    assert emoji_key("❤️") == "❤"


def test_normalize_remove_variation_selector_e_zwj_orfao():
    assert normalize_unicode_emoji(f"❤{VS}") == "❤"
    assert normalize_unicode_emoji(f"👍{VS}{ZWJ}") == "👍"
    assert normalize_unicode_emoji(f"👍{ZWJ}") == "👍"


def test_normalize_preserva_zwj_no_meio():
    # Família é uma sequência ZWJ legítima: o ZWJ interno não pode sumir.
    familia = f"👨{ZWJ}👩{ZWJ}👧"
    assert normalize_unicode_emoji(familia) == familia


def test_parse_emoji_normaliza_na_criacao():
    com_vs = parse_emoji(f"❤{VS}")
    sem_vs = parse_emoji("❤")

    assert com_vs is not None and sem_vs is not None
    assert com_vs.name == sem_vs.name == "❤"
    assert emoji_key(com_vs) == emoji_key(sem_vs)


async def test_contador_criado_com_vs_e_achado_sem_vs(db):
    emoji = parse_emoji(f"❤{VS}")
    assert emoji is not None
    criado = await create_counter(db, GUILD, "coracao", emoji, CANAL)

    achado = await get_counter_by_emoji(db, GUILD, "❤")
    assert achado is not None and achado.id == criado.id


async def test_contador_criado_sem_vs_e_achado_com_vs(db):
    """O caso real: o cliente de quem reagiu manda o emoji com U+FE0F."""
    emoji = parse_emoji("❤")
    assert emoji is not None
    criado = await create_counter(db, GUILD, "coracao", emoji, CANAL)

    achado = await get_counter_by_emoji(db, GUILD, f"❤{VS}")
    assert achado is not None and achado.id == criado.id


async def test_reacao_com_vs_incrementa_contador_criado_sem_vs(db):
    emoji = parse_emoji("❤")
    assert emoji is not None
    criado = await create_counter(db, GUILD, "coracao", emoji, CANAL)

    # É assim que a reação chega no payload do gateway.
    payload_emoji = discord.PartialEmoji(name=f"❤{VS}")
    achado = await get_counter_by_emoji(db, GUILD, emoji_key(payload_emoji))
    assert achado is not None

    assert await record_hit(db, achado.id, MENSAGEM, ANA, BRUNO) is True
    assert await counter_total(db, criado.id) == 1


# ---------------------------------------------------------------------------
# Emoji: unicode e custom
# ---------------------------------------------------------------------------


def test_emoji_key_unicode():
    assert emoji_key("🪱") == "🪱"


def test_emoji_key_custom_usa_o_id():
    # O id sobrevive a renomear o emoji no servidor; o nome, não.
    assert emoji_key("<:minhoca:1234567890123456789>") == "custom:1234567890123456789"
    assert emoji_key("<:verme:1234567890123456789>") == "custom:1234567890123456789"
    assert emoji_key("<a:minhoca_animada:9876543210987654321>") == "custom:9876543210987654321"


def test_parse_emoji_rejeita_texto_comum():
    assert parse_emoji("papoi") is None
    assert parse_emoji("") is None
    assert parse_emoji("   ") is None


def test_parse_emoji_aceita_unicode_e_custom():
    unicode_emoji = parse_emoji("🪱")
    assert unicode_emoji is not None and unicode_emoji.id is None

    custom = parse_emoji("<:minhoca:1234567890123456789>")
    assert custom is not None and custom.id == 1234567890123456789
    assert isinstance(custom, discord.PartialEmoji)


async def test_contador_encontrado_pelo_emoji_custom(db):
    emoji = parse_emoji("<:minhoca:1234567890123456789>")
    assert emoji is not None
    criado = await create_counter(db, GUILD, "minhocou", emoji, CANAL)

    achado = await get_counter_by_emoji(db, GUILD, "custom:1234567890123456789")
    assert achado is not None and achado.id == criado.id

    # Emoji igual, guild diferente: não é o mesmo contador.
    assert await get_counter_by_emoji(db, OUTRA_GUILD, "custom:1234567890123456789") is None


# ---------------------------------------------------------------------------
# Migrations
# ---------------------------------------------------------------------------


async def test_migration_normaliza_emoji_key_ja_gravada(tmp_path):
    """Contador criado antes da normalização precisa passar a receber reações."""
    db = Database(tmp_path / "m.db", pool_size=2)
    await db.connect()
    await init_db(db, MIGRATIONS[:3])  # schema anterior à normalização

    await db.execute(
        "INSERT INTO counters"
        " (guild_id, name, emoji_key, emoji_display, scoreboard_channel_id)"
        " VALUES (?, ?, ?, ?, ?)",
        (GUILD, "coracao", f"❤{VS}", f"❤{VS}", CANAL),
    )

    await init_db(db)  # aplica as migrations novas

    row = await db.fetchone("SELECT emoji_key FROM counters WHERE name = 'coracao'")
    assert row is not None and row["emoji_key"] == "❤"
    assert await get_counter_by_emoji(db, GUILD, f"❤{VS}") is not None
    await db.close()


async def test_migration_nao_toca_em_emoji_custom(tmp_path):
    db = Database(tmp_path / "m2.db", pool_size=2)
    await db.connect()
    await init_db(db, MIGRATIONS[:3])

    await db.execute(
        "INSERT INTO counters"
        " (guild_id, name, emoji_key, emoji_display, scoreboard_channel_id)"
        " VALUES (?, ?, ?, ?, ?)",
        (GUILD, "minhocou", "custom:1234567890123456789", "<:m:1234567890123456789>", CANAL),
    )

    await init_db(db)

    row = await db.fetchone("SELECT emoji_key FROM counters WHERE name = 'minhocou'")
    assert row is not None and row["emoji_key"] == "custom:1234567890123456789"
    await db.close()


async def test_migration_apaga_gatilhos_globais(tmp_path):
    db = Database(tmp_path / "g.db", pool_size=2)
    await db.connect()
    await init_db(db, MIGRATIONS[:3])

    globais = await db.fetchall("SELECT word FROM triggers WHERE guild_id = 0")
    assert [row["word"] for row in globais] == ["papoi"], "o seed antigo era global"

    await init_db(db)

    restantes = await db.fetchall("SELECT word FROM triggers WHERE guild_id = 0")
    assert restantes == []
    await db.close()


# ---------------------------------------------------------------------------
# Seed por guild
# ---------------------------------------------------------------------------


async def test_seed_cria_papoi_em_guild_vazia(db):
    assert await seed_guild_triggers(db, GUILD) == ["papoi"]

    triggers = await list_triggers(db, GUILD)
    assert [t.word for t in triggers] == ["papoi"]
    assert triggers[0].url.startswith("https://")
    assert triggers[0].guild_id == GUILD, "o seed é da guild, não global"


async def test_seed_nao_roda_se_a_guild_ja_tem_gatilho(db):
    await upsert_trigger(db, GUILD, "outro", "https://exemplo.com", False)

    assert await seed_guild_triggers(db, GUILD) == []
    assert {t.word for t in await list_triggers(db, GUILD)} == {"outro"}


async def test_seed_e_idempotente(db):
    await seed_guild_triggers(db, GUILD)
    assert await seed_guild_triggers(db, GUILD) == []
    assert len(await list_triggers(db, GUILD)) == 1


# ---------------------------------------------------------------------------
# Gatilhos de texto
# ---------------------------------------------------------------------------


def test_match_palavra_inteira_e_case_insensitive():
    gatilho = _trigger("papoi", substring=False)

    assert find_match([gatilho], "olha o papoi ali") is not None
    assert find_match([gatilho], "PAPOI!") is not None
    assert find_match([gatilho], "Papoi, e aí?") is not None
    assert find_match([gatilho], "(papoi)") is not None


def test_match_palavra_inteira_nao_casa_no_meio():
    gatilho = _trigger("papoi", substring=False)

    assert find_match([gatilho], "papoice") is None
    assert find_match([gatilho], "antipapoi") is None
    assert find_match([gatilho], "papoizinho") is None


def test_match_substring_casa_no_meio():
    gatilho = _trigger("papoi", substring=True)

    assert find_match([gatilho], "papoizinho") is not None
    assert find_match([gatilho], "ANTIPAPOICE") is not None


def test_match_devolve_apenas_um_gatilho():
    a = _trigger("papoi", substring=False, id_=1)
    b = _trigger("minhoca", substring=False, id_=2)

    achado = find_match([a, b], "papoi e minhoca na mesma frase")
    assert achado is not None and achado.id == 1


def test_palavra_com_regex_dentro_nao_quebra():
    gatilho = _trigger("c++", substring=False)

    assert find_match([gatilho], "adoro c++ demais") is not None
    assert find_match([gatilho], "adoro python") is None


def test_compile_trigger_e_cacheado():
    assert compile_trigger("papoi", False) is compile_trigger("papoi", False)
    assert compile_trigger("papoi", False) is not compile_trigger("papoi", True)


async def test_upsert_cria_e_depois_atualiza(db):
    assert await upsert_trigger(db, GUILD, "minhoca", "https://a.com", False) == "criado"
    assert await upsert_trigger(db, GUILD, "minhoca", "https://b.com", True) == "atualizado"

    minhoca = next(t for t in await list_triggers(db, GUILD) if t.word == "minhoca")
    assert minhoca.url == "https://b.com"
    assert minhoca.substring is True


async def test_remove_apaga_so_o_gatilho_da_guild(db):
    await upsert_trigger(db, GUILD, "papoi", "https://a.com", False)
    await upsert_trigger(db, OUTRA_GUILD, "papoi", "https://b.com", False)

    assert await remove_trigger(db, GUILD, "papoi") == 1

    assert await list_triggers(db, GUILD) == []
    outros = await list_triggers(db, OUTRA_GUILD)
    assert [t.word for t in outros] == ["papoi"], "a outra guild não pode ser afetada"


async def test_remove_inexistente_devolve_zero(db):
    assert await remove_trigger(db, GUILD, "naoexiste") == 0


async def test_gatilhos_nao_vazam_entre_guilds(db):
    await upsert_trigger(db, GUILD, "minhoca", "https://a.com", False)

    assert await list_triggers(db, OUTRA_GUILD) == []


# ---------------------------------------------------------------------------
# Estado em memória do cog: debounce, cooldowns e locks
# ---------------------------------------------------------------------------


async def test_debounce_agrupa_rajada_em_uma_publicacao(db, monkeypatch):
    monkeypatch.setattr(piadas, "SCOREBOARD_DEBOUNCE_SECONDS", 0.05)
    cog = Piadas(_BotFake(db))
    publicados: list[int] = []

    async def publicar(counter_id: int) -> None:
        publicados.append(counter_id)

    cog.publish_scoreboard = publicar  # type: ignore[assignment]

    for _ in range(5):  # cinco pessoas reagindo em sequência
        cog.schedule_scoreboard(7)
        await asyncio.sleep(0)

    await asyncio.sleep(0.2)

    assert publicados == [7], "a rajada precisa virar uma edição só"
    assert cog._pending_scoreboards == {}, "a task terminada sai do dict"


async def test_debounce_publica_de_novo_apos_a_janela(db, monkeypatch):
    monkeypatch.setattr(piadas, "SCOREBOARD_DEBOUNCE_SECONDS", 0.05)
    cog = Piadas(_BotFake(db))
    publicados: list[int] = []

    async def publicar(counter_id: int) -> None:
        publicados.append(counter_id)

    cog.publish_scoreboard = publicar  # type: ignore[assignment]

    cog.schedule_scoreboard(7)
    await asyncio.sleep(0.2)
    cog.schedule_scoreboard(7)
    await asyncio.sleep(0.2)

    assert publicados == [7, 7]


async def test_cog_unload_cancela_task_pendente(db):
    cog = Piadas(_BotFake(db))
    cog.schedule_scoreboard(7)
    task = cog._pending_scoreboards[7]

    cog.cog_unload()
    await asyncio.sleep(0)

    assert task.cancelled() or task.done(), "/reload não pode deixar task órfã"
    assert cog._pending_scoreboards == {}
    assert cog._scoreboard_locks == {}


async def test_publish_scoreboard_nao_cria_lock_para_contador_inexistente(db):
    cog = Piadas(_BotFake(db))

    await cog.publish_scoreboard(999999)

    assert cog._scoreboard_locks == {}, "id inválido não pode fazer o dict crescer"


async def test_remover_contador_limpa_lock_e_task(db, contador):
    cog = Piadas(_BotFake(db))
    cog.schedule_scoreboard(contador.id)
    cog._scoreboard_locks[contador.id] = asyncio.Lock()

    await delete_counter(db, contador.id)
    cog._cancel_scoreboard(contador.id)
    cog._scoreboard_locks.pop(contador.id, None)

    assert cog._pending_scoreboards == {}
    assert cog._scoreboard_locks == {}


def test_prune_cooldowns_descarta_entradas_vencidas(db):
    cog = Piadas(_BotFake(db))
    agora = time.monotonic()

    cog._cooldowns[(1, 1)] = agora - piadas.COOLDOWN_TTL_SECONDS - 60  # vencida
    cog._cooldowns[(2, 2)] = agora  # recente
    cog._last_cooldown_prune = 0.0  # força a limpeza nesta chamada

    cog._prune_cooldowns(agora)

    assert (1, 1) not in cog._cooldowns
    assert (2, 2) in cog._cooldowns


def test_prune_cooldowns_nao_roda_a_cada_mensagem(db):
    cog = Piadas(_BotFake(db))
    agora = time.monotonic()
    cog._last_cooldown_prune = agora
    cog._cooldowns[(1, 1)] = agora - piadas.COOLDOWN_TTL_SECONDS - 60

    cog._prune_cooldowns(agora)

    assert (1, 1) in cog._cooldowns, "a poda é periódica, não a cada acesso"


def _trigger(word: str, *, substring: bool, id_: int = 1):
    from leviathan.cogs.piadas import Trigger

    return Trigger(
        id=id_,
        guild_id=GUILD,
        word=word,
        url="https://exemplo.com",
        substring=substring,
    )
