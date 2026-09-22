"""Gera previews do card de citação em ``preview/``, sem subir o bot.

    uv run python scripts/preview_quote.py

Serve para conferir o visual depois de mexer nas constantes de layout de
``leviathan/cogs/quotes.py``. A pasta ``preview/`` é ignorada pelo git.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))

import urllib.error  # noqa: E402
import urllib.request  # noqa: E402

from PIL import Image, ImageDraw  # noqa: E402

from leviathan.cogs.quotes import (  # noqa: E402
    EMOJI_CACHE_DIR,
    LIMITE_CARACTERES,
    refs_de,
    render_quote_card,
    truncar,
)

SAIDA = RAIZ / "preview"

DATA = datetime(2026, 9, 14, 21, 30, tzinfo=timezone.utc)

CURTO = "Não é bug, é feature."

MEDIO = (
    "Passei três horas procurando o erro e no fim era um ponto e vírgula no lugar "
    "errado. Aprendi mais nessas três horas do que no semestre inteiro, mas ainda "
    "assim eu queria essas três horas de volta."
)

# Exatamente 400 caracteres: o limite, que NÃO deve ser truncado.
LONGO = (
    "A regra é clara: se o código funciona e ninguém sabe explicar por quê, "
    "ninguém encosta. Documentar é bom, testar é melhor, mas nada supera o medo "
    "coletivo de tocar naquele módulo que ninguém entende desde que o estagiário "
    "que escreveu foi embora. Ele deixou um comentário só, logo na primeira linha "
    "e o comentário dizia apenas assim: boa sorte para quem vier depois de mim, "
    "vocês vão precisar de toda"
)

# Acima do limite: precisa sair com reticências no fim.
ESTOURADO = LONGO + (
    " a sorte do mundo, porque aqui embaixo não tem nada além de gambiarra "
    "sustentando gambiarra desde 2019."
)


EMOJI_UNICODE = "Deu verde no CI 🎉 depois de 14 tentativas 😅 e um café ☕"

# Sequência ZWJ (família) e modificador de tom de pele: o caso que quebra quem
# segmenta emoji caractere a caractere.
EMOJI_ZWJ = "Fim de semana com a família 👨‍👩‍👧‍👦 e o deploy pode esperar 👍🏽"

# O primeiro id está no cache (semeado abaixo) e renderiza; o segundo não existe,
# então cai para o texto :nome: em vez de virar quadrado.
EMOJI_CUSTOM = (
    "Reação obrigatória: <:leviathan:1111111111111111111> "
    "e a que não baixou <:sumido:2222222222222222222>"
)

#: Id do emoji custom que o preview semeia no cache para exercitar o render.
CUSTOM_SEMEADO = "1111111111111111111"


def preparar_emojis_sync(texto: str) -> dict[str, bytes]:
    """Versão síncrona do download de sprites, para rodar fora do bot.

    Usa o mesmo ``refs_de`` e o mesmo cache em disco do cog, então o que aparece
    aqui é o que vai aparecer no Discord.
    """
    imagens: dict[str, bytes] = {}
    for chave, ref in refs_de(texto).items():
        caminho = EMOJI_CACHE_DIR / f"{chave}.png"
        if caminho.exists():
            imagens[chave] = caminho.read_bytes()
            continue
        for url in ref.urls:
            try:
                with urllib.request.urlopen(url, timeout=20) as resposta:
                    if resposta.status != 200:
                        continue
                    dados = resposta.read()
            except (urllib.error.URLError, OSError):
                continue
            EMOJI_CACHE_DIR.mkdir(parents=True, exist_ok=True)
            caminho.write_bytes(dados)
            imagens[chave] = dados
            break
        else:
            print(f"    sem sprite para {chave} -> cai para {ref.fallback!r}")
    return imagens


def semear_emoji_custom() -> None:
    """Grava no cache um sprite para o emoji custom fictício do preview.

    Um id inventado daria 404 no CDN do Discord. Semear o cache exercita o mesmo
    caminho de render que um emoji real do servidor seguiria.
    """
    caminho = EMOJI_CACHE_DIR / f"c-{CUSTOM_SEMEADO}.png"
    if caminho.exists():
        return
    sprite = Image.new("RGBA", (72, 72), (0, 0, 0, 0))
    desenho = ImageDraw.Draw(sprite)
    desenho.ellipse((2, 2, 69, 69), fill=(88, 101, 242, 255))
    desenho.ellipse((20, 24, 32, 40), fill=(255, 255, 255, 255))
    desenho.ellipse((40, 24, 52, 40), fill=(255, 255, 255, 255))
    desenho.arc((18, 34, 54, 60), start=10, end=170, fill=(255, 255, 255, 255), width=6)
    EMOJI_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    caminho.write_bytes(_png(sprite))


def _png(imagem: Image.Image) -> bytes:
    buffer = BytesIO()
    imagem.save(buffer, format="PNG")
    return buffer.getvalue()


def avatar_falso() -> bytes:
    """Avatar sintético, para conferir o recorte circular sem depender da rede."""
    imagem = Image.new("RGB", (256, 256), (61, 90, 128))
    desenho = ImageDraw.Draw(imagem)
    desenho.ellipse((60, 40, 196, 176), fill=(238, 230, 220))
    desenho.ellipse((20, 160, 236, 400), fill=(238, 230, 220))
    buffer = BytesIO()
    imagem.save(buffer, format="PNG")
    return buffer.getvalue()


def main() -> int:
    SAIDA.mkdir(exist_ok=True)
    avatar = avatar_falso()
    semear_emoji_custom()

    casos = [
        ("1-curto", CURTO, avatar),
        ("2-medio", MEDIO, avatar),
        ("3-longo-400", LONGO, avatar),
        ("4-estourado", ESTOURADO, avatar),
        ("5-sem-avatar", MEDIO, None),
        ("6-emoji-unicode", EMOJI_UNICODE, avatar),
        ("7-emoji-zwj", EMOJI_ZWJ, avatar),
        ("8-emoji-custom", EMOJI_CUSTOM, avatar),
    ]
    assert len(LONGO) == 400, f"o caso do limite precisa ter 400 chars, tem {len(LONGO)}"

    print(f"limite de truncamento: {LIMITE_CARACTERES} caracteres\n")
    for nome, texto, bytes_avatar in casos:
        emojis = preparar_emojis_sync(texto)
        png = render_quote_card(
            texto=texto,
            autor="João Vítor",
            data=DATA,
            canal="geral",
            avatar_bytes=bytes_avatar,
            emoji_imagens=emojis,
        )
        destino = SAIDA / f"quote-{nome}.png"
        destino.write_bytes(png)

        truncado = len(texto) > LIMITE_CARACTERES
        print(
            f"{destino.name:26s} {len(texto):4d} chars"
            f" | emoji: {len(emojis)}"
            f" | truncado: {'sim' if truncado else 'nao':3s}"
            f" | {len(png) / 1024:6.1f} KB"
        )
        if truncado:
            print(f"{'':26s} termina em: ...{truncar(texto)[-40:]!r}")

    print(f"\nAbra os arquivos em {SAIDA}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
