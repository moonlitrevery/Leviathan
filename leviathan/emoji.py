"""Helpers de emoji compartilhados entre cogs.

Fica fora de ``leviathan/cogs`` de propósito: o ``setup_hook`` varre aquela pasta e
tentaria carregar este módulo como extensão.

O ponto central é :func:`normalize_unicode_emoji`. O mesmo emoji chega de formas
diferentes conforme o cliente que enviou — com ou sem variation selector (U+FE0F), às
vezes com um ZWJ (U+200D) sobrando no fim — e comparar as formas cruas faria a
reação simplesmente não casar, sem erro nenhum no log.
"""

from __future__ import annotations

import discord

#: Caracteres invisíveis que variam conforme o cliente.
VARIATION_SELECTOR = "️"
ZERO_WIDTH_JOINER = "‍"

#: Quantos caracteres uma sequência unicode de emoji pode ter, no máximo.
MAX_CARACTERES_EMOJI = 16


def normalize_unicode_emoji(raw: str) -> str:
    """Forma canônica de um emoji unicode.

    Remove todo U+FE0F e o U+200D órfão no fim. O ZWJ no meio é preservado: ele é
    parte legítima de sequências como 👨‍👩‍👧.
    """
    return raw.replace(VARIATION_SELECTOR, "").rstrip(ZERO_WIDTH_JOINER)


def emoji_key(emoji: discord.PartialEmoji | discord.Emoji | str) -> str:
    """Chave estável de um emoji.

    Emoji custom vira ``custom:<id>``, que sobrevive a renomear o emoji no servidor;
    emoji unicode é a sequência de caracteres já normalizada.
    """
    if isinstance(emoji, str):
        emoji = discord.PartialEmoji.from_str(emoji)
    if emoji.id is not None:
        return f"custom:{emoji.id}"
    return normalize_unicode_emoji(emoji.name or "")


def parse_emoji(valor: str) -> discord.PartialEmoji | None:
    """Interpreta o que o usuário digitou como emoji, ou ``None`` se não for um.

    O emoji unicode volta já normalizado, para que a gravação use exatamente a mesma
    forma que :func:`emoji_key` produz na leitura de uma reação.
    """
    valor = valor.strip()
    if not valor:
        return None

    emoji = discord.PartialEmoji.from_str(valor)
    if emoji.id is not None:
        return emoji

    # Sem id é unicode: exigimos ao menos um caractere fora do ASCII, senão qualquer
    # palavra digitada por engano viraria um "emoji" válido.
    nome = normalize_unicode_emoji(emoji.name or "")
    if not nome or nome.isascii() or len(nome) > MAX_CARACTERES_EMOJI:
        return None
    return discord.PartialEmoji(name=nome)
