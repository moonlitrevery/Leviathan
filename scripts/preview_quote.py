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

from PIL import Image, ImageDraw  # noqa: E402

from leviathan.cogs.quotes import (  # noqa: E402
    LIMITE_CARACTERES,
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

    casos = [
        ("1-curto", CURTO, avatar),
        ("2-medio", MEDIO, avatar),
        ("3-longo-400", LONGO, avatar),
        ("4-estourado", ESTOURADO, avatar),
        ("5-sem-avatar", MEDIO, None),
    ]
    assert len(LONGO) == 400, f"o caso do limite precisa ter 400 chars, tem {len(LONGO)}"

    print(f"limite de truncamento: {LIMITE_CARACTERES} caracteres\n")
    for nome, texto, bytes_avatar in casos:
        png = render_quote_card(
            texto=texto,
            autor="João Vítor",
            data=DATA,
            canal="geral",
            avatar_bytes=bytes_avatar,
        )
        destino = SAIDA / f"quote-{nome}.png"
        destino.write_bytes(png)

        truncado = len(texto) > LIMITE_CARACTERES
        print(
            f"{destino.name:26s} {len(texto):4d} chars"
            f" | truncado: {'sim' if truncado else 'nao':3s}"
            f" | {len(png) / 1024:6.1f} KB"
        )
        if truncado:
            print(f"{'':26s} termina em: ...{truncar(texto)[-40:]!r}")

    print(f"\nAbra os arquivos em {SAIDA}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
