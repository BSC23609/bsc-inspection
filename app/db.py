"""Neon Postgres access via asyncpg. A small lazily-created pool is reused across
warm serverless invocations. Mirrors db.js (SSL required, no cert verification)."""
import ssl
import json
import asyncio
from urllib.parse import urlsplit, urlunsplit
import asyncpg
from .config import DATABASE_URL

_pool: asyncpg.Pool | None = None
_lock = asyncio.Lock()


async def _init_con(con):
    # decode jsonb/json columns straight to Python objects
    for t in ("jsonb", "json"):
        await con.set_type_codec(t, encoder=json.dumps, decoder=json.loads, schema="pg_catalog")


def _dsn_and_ssl():
    """asyncpg doesn't understand libpq's ?sslmode=; strip it and supply an SSL
    context that requires TLS but doesn't verify (same as pg's rejectUnauthorized:false)."""
    parts = urlsplit(DATABASE_URL)
    is_local = parts.hostname in ("localhost", "127.0.0.1")
    dsn = urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))  # drop query
    if is_local or not DATABASE_URL:
        return dsn, False
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return dsn, ctx


async def pool() -> asyncpg.Pool:
    global _pool
    if _pool is not None:
        return _pool
    async with _lock:
        if _pool is None:
            dsn, sslv = _dsn_and_ssl()
            _pool = await asyncpg.create_pool(
                dsn, ssl=sslv, min_size=1, max_size=4, command_timeout=30, init=_init_con,
            )
    return _pool


async def fetch(sql: str, *args):
    p = await pool()
    async with p.acquire() as con:
        return await con.fetch(sql, *args)


async def fetchrow(sql: str, *args):
    p = await pool()
    async with p.acquire() as con:
        return await con.fetchrow(sql, *args)


async def fetchval(sql: str, *args):
    p = await pool()
    async with p.acquire() as con:
        return await con.fetchval(sql, *args)


async def execute(sql: str, *args):
    p = await pool()
    async with p.acquire() as con:
        return await con.execute(sql, *args)
