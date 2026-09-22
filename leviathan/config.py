"""Leitura e validação das variáveis de ambiente do Leviathan Bot.

Toda falha de configuração vira uma ``ConfigError`` com mensagem legível,
para que o boot morra explicando o que falta em vez de cuspir um traceback.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

#: Raiz do projeto (a pasta que contém ``leviathan/``, ``.env`` etc.).
PROJECT_ROOT = Path(__file__).resolve().parent.parent

#: Usado quando ``DATABASE_PATH`` não está definido no ambiente.
DEFAULT_DATABASE_PATH = Path("data/leviathan.db")


class ConfigError(RuntimeError):
    """Configuração ausente ou inválida, com mensagem pronta para o usuário."""


@dataclass(frozen=True, slots=True)
class Config:
    """Configuração validada do bot."""

    discord_token: str
    guild_id: int
    database_path: Path
    #: Opcional: sem ela o cog lastfm não carrega e o resto do bot sobe normal.
    lastfm_api_key: str | None

    @classmethod
    def load(cls, *, env_file: Path | None = None) -> "Config":
        """Lê o ``.env`` + ambiente e devolve a configuração validada.

        Levanta :class:`ConfigError` listando *todos* os problemas de uma vez,
        para não obrigar a descobrir uma variável faltante por execução.
        """
        load_dotenv(env_file or (PROJECT_ROOT / ".env"))

        problems: list[str] = []

        token = os.getenv("DISCORD_TOKEN", "").strip()
        if not token:
            problems.append(
                "DISCORD_TOKEN não definido — copie o token do bot em "
                "https://discord.com/developers/applications (aba Bot > Reset Token)."
            )

        guild_id = 0
        raw_guild = os.getenv("GUILD_ID", "").strip()
        if not raw_guild:
            problems.append(
                "GUILD_ID não definido — ative o Modo Desenvolvedor no Discord, "
                "clique com o botão direito no servidor e use 'Copiar ID do servidor'."
            )
        else:
            try:
                guild_id = int(raw_guild)
            except ValueError:
                problems.append(
                    f"GUILD_ID precisa ser um número inteiro, recebido: {raw_guild!r}."
                )
            else:
                if guild_id <= 0:
                    problems.append(
                        f"GUILD_ID precisa ser um ID positivo, recebido: {guild_id}."
                    )

        raw_database = os.getenv("DATABASE_PATH", "").strip()
        database_path = Path(raw_database).expanduser() if raw_database else DEFAULT_DATABASE_PATH
        if not database_path.is_absolute():
            database_path = PROJECT_ROOT / database_path

        # Opcional de propósito: quem não usa Last.fm não precisa de chave.
        lastfm_api_key = os.getenv("LASTFM_API_KEY", "").strip() or None

        if problems:
            raise ConfigError(_format_problems(problems))

        return cls(
            discord_token=token,
            guild_id=guild_id,
            database_path=database_path,
            lastfm_api_key=lastfm_api_key,
        )


def _format_problems(problems: list[str]) -> str:
    linhas = "\n".join(f"  - {problema}" for problema in problems)
    return (
        "Configuração inválida. Corrija o arquivo .env na raiz do projeto "
        "(use .env.example como base):\n"
        f"{linhas}"
    )
