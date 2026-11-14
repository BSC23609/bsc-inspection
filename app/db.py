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
                # Neon pools connections through pgbouncer. asyncpg caches prepared
                # statement plans per connection; after an ALTER TABLE the cached plan
                # goes stale and every reused query raises InvalidCachedStatementError,
                # taking the whole site down. Disabling the cache makes each query
                # prepare fresh — no stale plans, immune to future schema changes.
                statement_cache_size=0,
            )
    return _pool


def _is_stale_plan(e: Exception) -> bool:
    return isinstance(e, asyncpg.exceptions.InvalidCachedStatementError)


async def _run(method: str, sql: str, *args):
    """Acquire a connection and run con.<method>(sql, *args). If a stale cached plan
    slips through (a warm connection prepared before a schema change), drop that
    connection from the pool and retry once on a fresh one."""
    p = await pool()
    for attempt in range(2):
        con = await p.acquire()
        try:
            return await getattr(con, method)(sql, *args)
        except Exception as e:
            if _is_stale_plan(e) and attempt == 0:
                await p.release(con, timeout=5)
                con = None
                continue
            raise
        finally:
            if con is not None:
                await p.release(con)


async def fetch(sql: str, *args):
    return await _run("fetch", sql, *args)


async def fetchrow(sql: str, *args):
    return await _run("fetchrow", sql, *args)


async def fetchval(sql: str, *args):
    return await _run("fetchval", sql, *args)


async def execute(sql: str, *args):
    return await _run("execute", sql, *args)
