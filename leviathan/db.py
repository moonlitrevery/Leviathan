"""Camada de acesso ao SQLite: pool de conexões aiosqlite + migrations versionadas.

Cada feature futura adiciona uma entrada nova em :data:`MIGRATIONS` com a próxima
versão livre; :func:`init_db` aplica só o que ainda falta, em ordem e dentro de uma
transação por migration. Migrations já aplicadas nunca são reescritas: para mudar o
schema, adicione uma migration nova.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Iterable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import aiosqlite

log = logging.getLogger(__name__)

#: PRAGMAs aplicados em toda conexão do pool.
_CONNECTION_PRAGMAS: tuple[str, ...] = (
    "PRAGMA journal_mode = WAL",  # leitores não bloqueiam o escritor
    "PRAGMA foreign_keys = ON",
    "PRAGMA synchronous = NORMAL",
    "PRAGMA busy_timeout = 5000",
)


@dataclass(frozen=True, slots=True)
class Migration:
    """Um passo de schema, identificado por um número de versão crescente."""

    version: int
    name: str
    statements: tuple[str, ...] = field(default_factory=tuple)


# Histórico do schema. NUNCA edite nem remova uma migration já publicada:
# acrescente a próxima versão no fim da tupla. Exemplo de uma feature futura:
#
#     Migration(
#         version=2,
#         name="jokes",
#         statements=(
#             "CREATE TABLE jokes ("
#             "  id       INTEGER PRIMARY KEY AUTOINCREMENT,"
#             "  guild_id INTEGER NOT NULL,"
#             "  text     TEXT    NOT NULL"
#             ")",
#             "CREATE INDEX idx_jokes_guild ON jokes (guild_id)",
#         ),
#     ),
MIGRATIONS: tuple[Migration, ...] = (
    Migration(
        version=1,
        name="baseline",
        statements=(),  # fundação: apenas marca o banco como inicializado
    ),
)


class Database:
    """Pool simples de conexões aiosqlite.

    O SQLite serializa escritas, então um punhado de conexões já basta: o pool
    existe para que comandos concorrentes não disputem uma única conexão. As
    conexões ficam em autocommit; escritas com mais de um statement devem usar
    :meth:`transaction`.
    """

    def __init__(self, path: Path | str, *, pool_size: int = 4) -> None:
        if pool_size < 1:
            raise ValueError("pool_size precisa ser >= 1")
        self.path = Path(path)
        self.pool_size = pool_size
        self._pool: asyncio.Queue[aiosqlite.Connection] = asyncio.Queue(maxsize=pool_size)
        self._connections: list[aiosqlite.Connection] = []
        self._lock = asyncio.Lock()

    @property
    def is_connected(self) -> bool:
        return bool(self._connections)

    async def connect(self) -> None:
        """Abre as conexões do pool. Idempotente."""
        async with self._lock:
            if self._connections:
                return
            self.path.parent.mkdir(parents=True, exist_ok=True)
            for _ in range(self.pool_size):
                conn = await aiosqlite.connect(self.path, isolation_level=None)
                conn.row_factory = aiosqlite.Row
                for pragma in _CONNECTION_PRAGMAS:
                    await conn.execute(pragma)
                self._connections.append(conn)
                self._pool.put_nowait(conn)
            log.info("Banco aberto em %s (pool de %d conexões)", self.path, self.pool_size)

    async def close(self) -> None:
        """Fecha todas as conexões do pool. Idempotente."""
        async with self._lock:
            if not self._connections:
                return
            while not self._pool.empty():
                self._pool.get_nowait()
            for conn in self._connections:
                await conn.close()
            self._connections.clear()
            log.info("Banco fechado")

    @asynccontextmanager
    async def acquire(self) -> AsyncIterator[aiosqlite.Connection]:
        """Pega uma conexão emprestada do pool e a devolve ao sair do bloco."""
        if not self._connections:
            raise RuntimeError("Database.connect() precisa ser chamado antes de acquire()")
        conn = await self._pool.get()
        try:
            yield conn
        finally:
            self._pool.put_nowait(conn)

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[aiosqlite.Connection]:
        """Executa o bloco dentro de uma transação, com rollback em caso de erro."""
        async with self.acquire() as conn:
            await conn.execute("BEGIN")
            try:
                yield conn
            except BaseException:
                await conn.execute("ROLLBACK")
                raise
            else:
                await conn.execute("COMMIT")

    async def execute(self, sql: str, params: Sequence[Any] = ()) -> int:
        """Roda um statement de escrita e devolve o número de linhas afetadas."""
        async with self.acquire() as conn:
            cursor = await conn.execute(sql, params)
            try:
                return cursor.rowcount
            finally:
                await cursor.close()

    async def executemany(self, sql: str, params: Iterable[Sequence[Any]]) -> None:
        async with self.transaction() as conn:
            await conn.executemany(sql, params)

    async def fetchone(self, sql: str, params: Sequence[Any] = ()) -> aiosqlite.Row | None:
        async with self.acquire() as conn:
            cursor = await conn.execute(sql, params)
            try:
                return await cursor.fetchone()
            finally:
                await cursor.close()

    async def fetchall(self, sql: str, params: Sequence[Any] = ()) -> list[aiosqlite.Row]:
        async with self.acquire() as conn:
            cursor = await conn.execute(sql, params)
            try:
                return list(await cursor.fetchall())
            finally:
                await cursor.close()


async def init_db(database: Database, migrations: Sequence[Migration] = MIGRATIONS) -> int:
    """Aplica as migrations pendentes e devolve a versão final do schema."""
    if not database.is_connected:
        await database.connect()

    _validate(migrations)

    async with database.acquire() as conn:
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_version (
                version    INTEGER PRIMARY KEY,
                name       TEXT    NOT NULL,
                applied_at TEXT    NOT NULL DEFAULT (datetime('now'))
            )
            """
        )
        cursor = await conn.execute("SELECT version FROM schema_version")
        try:
            applied = {row["version"] for row in await cursor.fetchall()}
        finally:
            await cursor.close()

    pending = [m for m in migrations if m.version not in applied]
    if not pending:
        current = max(applied, default=0)
        log.info("Schema já na versão %d, nenhuma migration pendente", current)
        return current

    for migration in pending:
        async with database.transaction() as conn:
            for statement in migration.statements:
                await conn.execute(statement)
            await conn.execute(
                "INSERT INTO schema_version (version, name) VALUES (?, ?)",
                (migration.version, migration.name),
            )
        log.info("Migration %d (%s) aplicada", migration.version, migration.name)

    current = max(applied | {m.version for m in pending})
    log.info("Schema do banco na versão %d", current)
    return current


def _validate(migrations: Sequence[Migration]) -> None:
    versions = [m.version for m in migrations]
    if len(set(versions)) != len(versions):
        raise ValueError("Há versões duplicadas em MIGRATIONS")
    if versions != sorted(versions):
        raise ValueError("MIGRATIONS precisa estar em ordem crescente de versão")
