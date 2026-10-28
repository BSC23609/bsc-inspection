"""Microsoft Graph helpers — OneDrive upload + read, using the same app
registration as the Node QMS (TENANT_ID / CLIENT_ID / CLIENT_SECRET / USER_ID)."""
import time
import asyncio
import httpx
from urllib.parse import quote
from .config import TENANT_ID, CLIENT_ID, CLIENT_SECRET, USER_ID

_GRAPH = "https://graph.microsoft.com/v1.0"

# Cache the Graph token in-process. A token is valid ~60-75 min, so re-using it
# (instead of a fresh OAuth round-trip on every upload/read) removes a network
# call from each OneDrive operation — a big speed-up on saves and backfills.
_token_cache = {"value": None, "exp": 0.0}


async def get_token() -> str:
    now = time.time()
    if _token_cache["value"] and now < _token_cache["exp"]:
        return _token_cache["value"]
    data = {
        "grant_type": "client_credentials",
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "scope": "https://graph.microsoft.com/.default",
    }
    url = f"https://login.microsoftonline.com/{TENANT_ID}/oauth2/v2.0/token"
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post(url, data=data)
        j = r.json()
    if "access_token" not in j:
        raise RuntimeError(f"Graph token error: {j}")
    tok = j["access_token"]
    try:
        ttl = float(j.get("expires_in", 3600))
    except (TypeError, ValueError):
        ttl = 3600.0
    _token_cache["value"] = tok
    _token_cache["exp"] = now + max(60.0, ttl - 120.0)   # refresh 2 min early
    return tok


def _item_url(path: str, suffix: str) -> str:
    return f"{_GRAPH}/users/{USER_ID}/drive/root:/{quote(path)}:{suffix}"


async def upload_file(token: str, path: str, content: bytes, content_type: str, tries: int = 3):
    """Upload to OneDrive, retrying transient failures (429 throttling, 423 locked,
    5xx, network blips) so a bulk run doesn't silently drop files. Small backoff so a
    page of uploads still fits Vercel's time limit."""
    url = _item_url(path, "/content")
    headers = {"Authorization": f"Bearer {token}", "Content-Type": content_type}
    last_err = None
    for attempt in range(max(1, tries)):
        try:
            async with httpx.AsyncClient(timeout=60) as c:
                r = await c.put(url, headers=headers, content=content)
            if r.status_code < 300:
                return
            if r.status_code in (423, 429, 500, 502, 503, 504) and attempt < tries - 1:
                try:
                    wait = float(r.headers.get("Retry-After", 0) or 0)
                except (TypeError, ValueError):
                    wait = 0.0
                await asyncio.sleep(min(wait or (1.2 * (attempt + 1)), 6.0))
                continue
            raise RuntimeError(f"Upload failed ({r.status_code}): {r.text}")
        except (httpx.TransportError, httpx.TimeoutException) as e:
            last_err = e
            if attempt < tries - 1:
                await asyncio.sleep(1.2 * (attempt + 1))
                continue
            raise
    if last_err:
        raise last_err


async def create_folder(token: str, parent_path: str, name: str) -> bool:
    """Create an (empty) folder `name` under `parent_path` in OneDrive. Idempotent:
    an existing folder is left untouched (409 Conflict is treated as success). The
    parent folder must already exist. Used to pre-create the per-machine report
    subfolders so each machine shows Daily / Quarterly / Breakdown even when empty."""
    url = f"{_GRAPH}/users/{USER_ID}/drive/root:/{quote(parent_path)}:/children"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    body = {"name": name, "folder": {}, "@microsoft.graph.conflictBehavior": "fail"}
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post(url, headers=headers, json=body)
    return r.status_code in (200, 201, 409)


async def list_children(token: str, path: str) -> list[dict]:
    """List the immediate children of a folder path. Returns [{id,name,is_folder}].
    Empty list on any failure (e.g. the folder doesn't exist)."""
    url = (f"{_GRAPH}/users/{USER_ID}/drive/root:/{quote(path)}:/children"
           f"?$select=id,name,folder,file&$top=999")
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.get(url, headers={"Authorization": f"Bearer {token}"})
    if r.status_code != 200:
        return []
    return [{"id": it.get("id"), "name": it.get("name"), "is_folder": ("folder" in it)}
            for it in (r.json().get("value") or [])]


async def move_item(token: str, item_id: str, target_parent_id: str) -> bool:
    """Move a drive item (file or folder) into the folder with id target_parent_id."""
    url = f"{_GRAPH}/users/{USER_ID}/drive/items/{item_id}"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.patch(url, headers=headers,
                          json={"parentReference": {"id": target_parent_id}})
    return r.status_code < 300


async def read_text(token: str, path: str) -> str | None:
    """Return a file's text content, or None if it doesn't exist."""
    url = _item_url(path, "/content")
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.get(url, headers={"Authorization": f"Bearer {token}"})
    if r.status_code == 200:
        return r.text
    return None


async def delete_item(token: str, item_id: str) -> bool:
    """Delete a drive item (file or folder) by id. Returns True on success (204) or
    if it was already gone (404). Used when an admin deletes a work order and its
    report PDF together."""
    url = f"{_GRAPH}/users/{USER_ID}/drive/items/{item_id}"
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.delete(url, headers={"Authorization": f"Bearer {token}"})
    return r.status_code in (204, 404)


async def download_item(token: str, item_id: str) -> bytes | None:
    """Return a drive item's raw bytes by its id, or None if it can't be fetched.
    Used by the Monitor's View / Download buttons to stream a report PDF."""
    url = f"{_GRAPH}/users/{USER_ID}/drive/items/{item_id}/content"
    async with httpx.AsyncClient(timeout=60, follow_redirects=True) as c:
        r = await c.get(url, headers={"Authorization": f"Bearer {token}"})
    return r.content if r.status_code == 200 else None


# ---- Workbook table helpers (append rows to an existing OneDrive .xlsx) ----
async def get_item_id(token: str, path: str) -> str | None:
    url = f"{_GRAPH}/users/{USER_ID}/drive/root:/{quote(path)}"
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.get(url, headers={"Authorization": f"Bearer {token}"})
    return r.json().get("id") if r.status_code == 200 else None


async def table_columns(token: str, item_id: str, table: str) -> list[str]:
    url = f"{_GRAPH}/users/{USER_ID}/drive/items/{item_id}/workbook/tables/{table}/columns?$select=name"
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.get(url, headers={"Authorization": f"Bearer {token}"})
    if r.status_code != 200:
        return []
    return [col.get("name", "") for col in (r.json().get("value") or [])]


async def ensure_table_columns(token: str, item_id: str, table: str, expected: list[str]):
    """Add any missing trailing columns so the table holds at least `expected`."""
    cur = await table_columns(token, item_id, table)
    added = 0
    async with httpx.AsyncClient(timeout=30) as c:
        for i in range(len(cur), len(expected)):
            r = await c.post(
                f"{_GRAPH}/users/{USER_ID}/drive/items/{item_id}/workbook/tables/{table}/columns",
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                json={"name": expected[i]})
            if r.status_code < 300:
                added += 1
            else:
                break
    return {"before": len(cur), "expected": len(expected), "added": added}


async def create_session(token: str, item_id: str) -> str | None:
    url = f"{_GRAPH}/users/{USER_ID}/drive/items/{item_id}/workbook/createSession"
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post(url, headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                         json={"persistChanges": True})
    return r.json().get("id") if r.status_code < 300 else None


async def close_session(token: str, item_id: str, session_id: str):
    try:
        async with httpx.AsyncClient(timeout=20) as c:
            await c.post(f"{_GRAPH}/users/{USER_ID}/drive/items/{item_id}/workbook/closeSession",
                         headers={"Authorization": f"Bearer {token}", "workbook-session-id": session_id})
    except Exception:
        pass


async def add_table_row(token: str, item_id: str, table: str, values: list):
    """Append one row ([[...]]) to a workbook table, using a short-lived session."""
    sid = await create_session(token, item_id)
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    if sid:
        headers["workbook-session-id"] = sid
    url = f"{_GRAPH}/users/{USER_ID}/drive/items/{item_id}/workbook/tables/{table}/rows/add"
    try:
        async with httpx.AsyncClient(timeout=90) as c:
            r = await c.post(url, headers=headers, json={"values": [values]})
        if r.status_code >= 300:
            raise RuntimeError(f"rows/add failed ({r.status_code}): {r.text[:300]}")
    finally:
        if sid:
            await close_session(token, item_id, sid)


async def read_table(token: str, item_id: str, table: str):
    """Return (column_names, rows) for a workbook table; ([],[]) on any failure."""
    cols = await table_columns(token, item_id, table)
    if not cols:
        return [], []
    url = (f"{_GRAPH}/users/{USER_ID}/drive/items/{item_id}/workbook/tables/{table}"
           f"/rows?$select=values&$top=5000")
    async with httpx.AsyncClient(timeout=60) as c:
        r = await c.get(url, headers={"Authorization": f"Bearer {token}"})
    if r.status_code != 200:
        return cols, []
    rows = []
    for item in (r.json().get("value") or []):
        vals = item.get("values") or [[]]
        rows.append(vals[0] if vals else [])
    return cols, rows
