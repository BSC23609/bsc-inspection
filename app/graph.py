"""Microsoft Graph helpers — OneDrive upload + read, using the same app
registration as the Node QMS (TENANT_ID / CLIENT_ID / CLIENT_SECRET / USER_ID)."""
import httpx
from urllib.parse import quote
from .config import TENANT_ID, CLIENT_ID, CLIENT_SECRET, USER_ID

_GRAPH = "https://graph.microsoft.com/v1.0"


async def get_token() -> str:
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
    return j["access_token"]


def _item_url(path: str, suffix: str) -> str:
    return f"{_GRAPH}/users/{USER_ID}/drive/root:/{quote(path)}:{suffix}"


_SIMPLE_MAX = 4 * 1024 * 1024 - 1024   # Graph simple PUT limit is 4 MB


async def upload_file(token: str, path: str, content: bytes, content_type: str):
    """Create/overwrite a OneDrive file. Uses an upload session above 4 MB
    (phone photos often exceed the simple-upload limit)."""
    if len(content) > _SIMPLE_MAX:
        return await _upload_large(token, path, content)
    url = _item_url(path, "/content")
    async with httpx.AsyncClient(timeout=60) as c:
        r = await c.put(url, headers={"Authorization": f"Bearer {token}",
                                      "Content-Type": content_type}, content=content)
    if r.status_code >= 300:
        raise RuntimeError(f"Upload failed ({r.status_code}): {r.text[:300]}")


async def _upload_large(token: str, path: str, content: bytes):
    url = _item_url(path, "/createUploadSession")
    async with httpx.AsyncClient(timeout=120) as c:
        r = await c.post(url, headers={"Authorization": f"Bearer {token}",
                                       "Content-Type": "application/json"},
                         json={"item": {"@microsoft.graph.conflictBehavior": "replace"}})
        if r.status_code >= 300:
            raise RuntimeError(f"Upload session failed ({r.status_code}): {r.text[:300]}")
        up = r.json()["uploadUrl"]
        chunk, total, pos = 5 * 320 * 1024, len(content), 0
        while pos < total:
            part = content[pos:pos + chunk]
            end = pos + len(part) - 1
            rr = await c.put(up, headers={"Content-Range": f"bytes {pos}-{end}/{total}",
                                          "Content-Length": str(len(part))}, content=part)
            if rr.status_code >= 300:
                raise RuntimeError(f"Chunk upload failed ({rr.status_code}): {rr.text[:300]}")
            pos = end + 1


async def read_text(token: str, path: str) -> str | None:
    """Return a file's text content, or None if it doesn't exist."""
    url = _item_url(path, "/content")
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.get(url, headers={"Authorization": f"Bearer {token}"})
    if r.status_code == 200:
        return r.text
    return None


# ---- Workbook table helpers (append rows to an existing OneDrive .xlsx) ----
async def get_item_id(token: str, path: str) -> str | None:
    url = f"{_GRAPH}/users/{USER_ID}/drive/root:/{quote(path)}"
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.get(url, headers={"Authorization": f"Bearer {token}"})
    return r.json().get("id") if r.status_code == 200 else None


async def table_columns(token: str, item_id: str, table: str) -> list[str]:
    """The table's real header row (every column, in order). Uses headerRowRange,
    which — unlike the /columns collection — always returns the full width."""
    base = f"{_GRAPH}/users/{USER_ID}/drive/items/{item_id}/workbook/tables/{table}"
    async with httpx.AsyncClient(timeout=60) as c:
        r = await c.get(f"{base}/headerRowRange?$select=values",
                        headers={"Authorization": f"Bearer {token}"})
        if r.status_code == 200:
            vals = r.json().get("values") or [[]]
            return ["" if v is None else str(v) for v in (vals[0] if vals else [])]
        r = await c.get(f"{base}/columns?$select=name&$top=2000",
                        headers={"Authorization": f"Bearer {token}"})
    if r.status_code != 200:
        return []
    return [col.get("name", "") for col in (r.json().get("value") or [])]


async def ensure_table_columns(token: str, item_id: str, table: str, expected: list[str]):
    """Add only the expected columns whose names the table doesn't have yet."""
    cur = await table_columns(token, item_id, table)
    have = {h.strip().lower() for h in cur}
    missing = [h for h in expected if h.strip().lower() not in have]
    added = 0
    async with httpx.AsyncClient(timeout=30) as c:
        for name in missing:
            r = await c.post(
                f"{_GRAPH}/users/{USER_ID}/drive/items/{item_id}/workbook/tables/{table}/columns",
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                json={"name": name})
            if r.status_code < 300:
                added += 1
            else:
                print(f"[graph] couldn't add column {name!r} to {table}: {r.text[:200]}")
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


async def require_item_id(token: str, path: str) -> str:
    """Like get_item_id but raises a readable error (file moved/renamed/deleted)."""
    url = f"{_GRAPH}/users/{USER_ID}/drive/root:/{quote(path)}"
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.get(url, headers={"Authorization": f"Bearer {token}"})
    if r.status_code == 200:
        return r.json()["id"]
    if r.status_code == 404:
        raise RuntimeError(f"Excel file not found on OneDrive: {path}")
    raise RuntimeError(f"Couldn't open {path} ({r.status_code}): {r.text[:200]}")


async def column_values(token: str, item_id: str, table: str, index: int) -> list:
    """All body values of one table column (header excluded) — cheap duplicate check."""
    url = (f"{_GRAPH}/users/{USER_ID}/drive/items/{item_id}/workbook/tables/{table}"
           f"/columns/itemAt(index={index})?$select=values")
    async with httpx.AsyncClient(timeout=60) as c:
        r = await c.get(url, headers={"Authorization": f"Bearer {token}"})
    if r.status_code != 200:
        raise RuntimeError(f"Couldn't read {table} column {index} ({r.status_code}): {r.text[:200]}")
    vals = r.json().get("values") or []
    return [(v[0] if v else "") for v in vals[1:]]


async def get_table_row(token: str, item_id: str, table: str, index: int) -> list:
    url = (f"{_GRAPH}/users/{USER_ID}/drive/items/{item_id}/workbook/tables/{table}"
           f"/rows/itemAt(index={index})?$select=values")
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.get(url, headers={"Authorization": f"Bearer {token}"})
    if r.status_code != 200:
        raise RuntimeError(f"Couldn't read {table} row {index} ({r.status_code}): {r.text[:200]}")
    vals = r.json().get("values") or [[]]
    return list(vals[0]) if vals else []


async def update_table_row(token: str, item_id: str, table: str, index: int, values: list):
    sid = await create_session(token, item_id)
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    if sid:
        headers["workbook-session-id"] = sid
    url = (f"{_GRAPH}/users/{USER_ID}/drive/items/{item_id}/workbook/tables/{table}"
           f"/rows/itemAt(index={index})")
    try:
        async with httpx.AsyncClient(timeout=90) as c:
            r = await c.patch(url, headers=headers, json={"values": [values]})
        if r.status_code >= 300:
            raise RuntimeError(f"row update failed ({r.status_code}): {r.text[:300]}")
    finally:
        if sid:
            await close_session(token, item_id, sid)


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
