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


async def upload_file(token: str, path: str, content: bytes, content_type: str):
    url = _item_url(path, "/content")
    async with httpx.AsyncClient(timeout=60) as c:
        r = await c.put(url, headers={"Authorization": f"Bearer {token}",
                                      "Content-Type": content_type}, content=content)
    if r.status_code >= 300:
        raise RuntimeError(f"Upload failed ({r.status_code}): {r.text}")


async def read_text(token: str, path: str) -> str | None:
    """Return a file's text content, or None if it doesn't exist."""
    url = _item_url(path, "/content")
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.get(url, headers={"Authorization": f"Bearer {token}"})
    if r.status_code == 200:
        return r.text
    return None
