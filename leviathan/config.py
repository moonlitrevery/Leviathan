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


def parse_guild_ids(bruto: str) -> tuple[tuple[int, ...], list[str]]:
    """Interpreta o ``GUILD_ID``: ``(ids, problemas)``.

    Aceita um id sozinho — que é como o arquivo sempre foi usado — ou vários
    separados por vírgula, com ou sem espaço em volta. Entrada vazia entre
    vírgulas é ignorada, para que uma vírgula sobrando no fim não vire erro.

    Cada entrada ruim vira um problema próprio, em vez de a primeira abortar a
    leitura: o mesmo motivo pelo qual :meth:`Config.load` junta tudo antes de
    reclamar — quem está configurando vê a lista inteira de uma vez.
    """
    ids: list[int] = []
    problemas: list[str] = []

    for entrada in bruto.split(","):
        entrada = entrada.strip()
        if not entrada:
            continue
        try:
            valor = int(entrada)
        except ValueError:
            problemas.append(
                f"GUILD_ID: {entrada!r} não é um número inteiro. Use um ID por "
                "vírgula, como 123456789 ou 123456789, 987654321."
            )
            continue
        if valor <= 0:
            problemas.append(
                f"GUILD_ID: {valor} não é um ID válido — IDs do Discord são "
                "números positivos."
            )
            continue
        # O mesmo servidor listado duas vezes sincronizaria duas vezes à toa.
        if valor not in ids:
            ids.append(valor)

    return tuple(ids), problemas


@dataclass(frozen=True, slots=True)
class Config:
    """Configuração validada do bot."""

    discord_token: str
    #: Um ou mais servidores onde os slash commands são sincronizados.
    guild_ids: tuple[int, ...]
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

        guild_ids: tuple[int, ...] = ()
        raw_guild = os.getenv("GUILD_ID", "").strip()
        if not raw_guild:
            problems.append(
                "GUILD_ID não definido — ative o Modo Desenvolvedor no Discord, "
                "clique com o botão direito no servidor e use 'Copiar ID do servidor'. "
                "Para mais de um servidor, separe os IDs por vírgula."
            )
        else:
            guild_ids, problemas_guild = parse_guild_ids(raw_guild)
            problems.extend(problemas_guild)
            if not guild_ids and not problemas_guild:
                problems.append(
                    f"GUILD_ID não tem nenhum ID utilizável, recebido: {raw_guild!r}."
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
            guild_ids=guild_ids,
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
