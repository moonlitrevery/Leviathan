"""Ponto de entrada: ``uv run python -m leviathan``."""

from __future__ import annotations

import logging
import sys

import discord

from leviathan.bot import LeviathanBot
from leviathan.config import Config, ConfigError

LOG_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

log = logging.getLogger("leviathan")


def setup_logging(level: int = logging.INFO) -> None:
    """Configura o logging da aplicação e cala o spam do gateway."""
    # No Windows a saída redirecionada cai no code page local e embaralha acentos.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    logging.basicConfig(
        level=level,
        format=LOG_FORMAT,
        datefmt=DATE_FORMAT,
        stream=sys.stdout,
    )
    # O gateway loga cada heartbeat e cada evento de shard em INFO/DEBUG.
    logging.getLogger("discord.gateway").setLevel(logging.WARNING)


def main() -> int:
    setup_logging()

    try:
        config = Config.load()
    except ConfigError as exc:
        # Erro de configuração é problema de setup, não bug: mensagem limpa.
        print(f"\n[Leviathan] {exc}\n", file=sys.stderr)
        return 1

    bot = LeviathanBot(config)
    try:
        # log_handler=None impede o discord.py de instalar o próprio handler.
        bot.run(config.discord_token, log_handler=None)
    except discord.LoginFailure:
        print(
            "\n[Leviathan] DISCORD_TOKEN inválido — gere um token novo em "
            "https://discord.com/developers/applications (aba Bot > Reset Token).\n",
            file=sys.stderr,
        )
        return 1
    except discord.PrivilegedIntentsRequired:
        print(
            "\n[Leviathan] Os intents privilegiados não estão habilitados. No portal do "
            "Discord, aba Bot, ligue 'MESSAGE CONTENT INTENT' e 'SERVER MEMBERS INTENT'.\n",
            file=sys.stderr,
        )
        return 1
    except KeyboardInterrupt:  # pragma: no cover - encerramento manual
        log.info("Encerrado pelo usuário")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
