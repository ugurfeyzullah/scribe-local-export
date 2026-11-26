#!/usr/bin/env python3
"""
amazon_cloud.py - wireless access to your Kindle library through your Amazon
account, the way the Kindle phone apps and Amazon's websites do it.

Nothing here talks to the device: the Scribe syncs to Amazon over Wi-Fi, and
this module reads what Amazon has. All endpoints are unofficial (the ones
Amazon's own web pages call) and can change without notice:

  Notebooks (Kindle for Web, all marketplaces are served by read.amazon.com)
    GET  https://read.amazon.com/kindle-notebook/api/notes        folder/notebook tree
    GET  https://read.amazon.com/openNotebook?notebookId=&marketplaceId=
                                                                  metadata + renderingToken
    GET  https://read.amazon.com/renderPage?startPage=&endPage=&width=&height=&dpi=
         header x-amzn-karamel-notebook-rendering-token           tar of PNG pages
  Documents ("Manage Your Content and Devices")
    GET  https://www.<store>/hz/mycd/digital-console/contentlist/pdocs/dateDsc/
                                                                  page with `csrfToken`
    POST https://www.<store>/hz/mycd/ajax  data=<json>&csrfToken=  OwnershipData / GetDevices
  Personal-document files themselves are NOT available: MYCD's
  SourceDownloadActivity answers success=false for them (also inside Amazon's
  own page), so documents are listed here and opened from the USB mirror.

Sign-in: `--login` opens Amazon's own sign-in page in a window (Edge WebView2
via pywebview). You type your password into Amazon's page; this program only
keeps the resulting session cookies, encrypted with Windows DPAPI so only your
Windows account can read them. Requests are spaced out like a browser's.

Prior art the protocol details were checked against: ata88/kindle-scribe-sync,
k4rnaj1k/obsidian-kindle-scribe-notes-sync-plugin, yihong0618/Kindle_download_helper.

CLI (used by server.py in a subprocess for the sign-in window):
    python amazon_cloud.py --login --store amazon.de
    python amazon_cloud.py --status
    python amazon_cloud.py --logout
    python amazon_cloud.py --list
"""

import io
import json
import logging
import re
import tarfile
import threading
import time
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)

APP_DIR = Path(__file__).resolve().parent
SESSION_PATH = APP_DIR / "amazon_session.bin"

READ_BASE = "https://read.amazon.com"
NOTES_API = READ_BASE + "/kindle-notebook/api/notes"
TOKEN_HEADER = "x-amzn-karamel-notebook-rendering-token"

STORES = ["amazon.com", "amazon.de", "amazon.co.uk", "amazon.fr", "amazon.it",
          "amazon.es", "amazon.nl", "amazon.ca", "amazon.com.au", "amazon.co.jp",
          "amazon.in", "amazon.com.br", "amazon.com.mx"]

# openNotebook wants the account's marketplace id; try the likely ones.
MARKETPLACE_IDS = {
    "amazon.com": "ATVPDKIKX0DER", "amazon.de": "A1PA6795UKMFR9",
    "amazon.co.uk": "A1F83G8C2ARO7P", "amazon.fr": "A13V1IB3VIYZZH",
    "amazon.it": "APJ6JRA9NG5V4", "amazon.es": "A1RKKUPIHCS9HS",
    "amazon.nl": "A1805IZSGTT6HS", "amazon.ca": "A2EUQ1WTGCTBG2",
    "amazon.com.au": "A39IBJ37TRP1C6", "amazon.co.jp": "A1VC38T7YXB528",
    "amazon.in": "A21TJRUUN4KGV", "amazon.com.br": "A2Q3Y263D00KWC",
    "amazon.com.mx": "A1AM78C64UM0Y8",
}

REQUEST_DELAY = 0.6     # seconds between requests, like a person clicking


class AuthExpired(Exception):
    """The Amazon session is missing or expired - sign in again."""


class CloudError(Exception):
    pass


# ── Session storage (DPAPI-encrypted) ────────────────────────────────────────

def _protect(data: bytes) -> bytes:
    import win32crypt
    return win32crypt.CryptProtectData(data, "Scribe Library Amazon session",
                                       None, None, None, 0)


def _unprotect(data: bytes) -> bytes:
    import win32crypt
    return win32crypt.CryptUnprotectData(data, None, None, None, 0)[1]


def load_session() -> dict | None:
    try:
        return json.loads(_unprotect(SESSION_PATH.read_bytes()).decode("utf-8"))
    except FileNotFoundError:
        return None
    except Exception as e:
        log.warning("Cannot read the saved Amazon session: %s", e)
        return None


def save_session(session: dict):
    tmp = SESSION_PATH.with_suffix(".tmp")
    tmp.write_bytes(_protect(json.dumps(session).encode("utf-8")))
    tmp.replace(SESSION_PATH)


def delete_session():
    SESSION_PATH.unlink(missing_ok=True)


def session_status() -> dict:
    s = load_session()
    if not s:
        return {"signed_in": False}
    return {"signed_in": True, "store": s.get("store"), "saved_at": s.get("saved_at"),
            "has_notebooks": s.get("notebooks_ok", False)}


# ── Sign-in window ───────────────────────────────────────────────────────────

def _morsels(cookies) -> list[dict]:
    """pywebview returns http.cookies.SimpleCookie objects."""
    out = []
    for jar in cookies or []:
        for name, m in jar.items():
            out.append({"name": name, "value": m.value,
                        "domain": m["domain"] or "", "path": m["path"] or "/",
                        "expires": m["expires"] or None})
    return out


def interactive_login(store: str, timeout: float = 900) -> dict:
    """Open Amazon's sign-in in a window; return the captured session.

    Step 1: the store's "Manage Your Content and Devices" page (documents).
    Step 2: read.amazon.com Kindle for Web notebooks (may ask again: it can
            be a different Amazon sign-in for non-US stores).
    """
    import webview

    if store not in STORES:
        raise CloudError(f"Unknown store {store!r}")
    mycd = f"https://www.{store}/hz/mycd/digital-console/contentlist/pdocs/dateDsc/"
    result = {"ok": False, "cookies": {}, "user_agent": None, "notebooks_ok": False,
              "error": None}

    window = webview.create_window(
        f"Sign in to {store} - Scribe Library", mycd, width=520, height=820)

    def collect():
        try:
            for c in _morsels(window.get_cookies()):
                result["cookies"][(c["name"], c["domain"], c["path"])] = c
        except Exception as e:
            log.debug("get_cookies failed: %s", e)

    def on_amazon_page(url, host_suffix, path_part):
        p = urllib.parse.urlparse(url or "")
        return (p.hostname or "").endswith(host_suffix) and path_part in p.path \
            and "/ap/" not in p.path

    def drive():
        deadline = time.time() + timeout
        stage = "mycd"
        try:
            while time.time() < deadline:
                time.sleep(1)
                try:
                    url = window.get_current_url()
                except Exception:
                    return  # window closed by the user
                if stage == "mycd" and on_amazon_page(url, store, "/hz/mycd"):
                    time.sleep(2)  # let the page finish setting cookies
                    collect()
                    result["user_agent"] = window.evaluate_js("navigator.userAgent")
                    stage = "notes"
                    window.load_url(NOTES_API)
                elif stage == "notes" and on_amazon_page(url, "read.amazon.com", "/kindle-notebook"):
                    time.sleep(2)
                    collect()
                    result["notebooks_ok"] = True
                    result["ok"] = True
                    window.destroy()
                    return
            result["error"] = "Sign-in timed out"
            if stage == "notes":   # documents worked; notebooks did not
                result["ok"] = True
            window.destroy()
        except Exception as e:
            result["error"] = str(e)
            try:
                window.destroy()
            except Exception:
                pass

    # private_mode=False keeps WebView2's own cookie store so a later sign-in
    # can be quicker; the session we use is the one we save below.
    webview.start(drive, private_mode=False)

    if not result["ok"]:
        raise CloudError(result["error"] or "Sign-in window was closed")
    session = {
        "store": store,
        "cookies": list(result["cookies"].values()),
        "user_agent": result["user_agent"],
        "notebooks_ok": result["notebooks_ok"],
        "marketplace_id": None,
        "saved_at": datetime.now(timezone.utc).isoformat(),
    }
    save_session(session)
    return session


# ── HTTP client ──────────────────────────────────────────────────────────────

class AmazonCloud:
    """Requests-based client over a saved session. Not thread-safe; the
    server serialises calls with a lock."""

    def __init__(self, session: dict | None = None):
        import requests
        self.data = session or load_session()
        if not self.data:
            raise AuthExpired("Not signed in")
        self.store = self.data["store"]
        self.http = requests.Session()
        self.http.headers["User-Agent"] = self.data.get("user_agent") or (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/130.0 Safari/537.36 Edg/130.0")
        self.http.headers["Accept-Language"] = "en-US,en;q=0.9,de;q=0.8"
        for c in self.data.get("cookies", []):
            self.http.cookies.set(c["name"], c["value"],
                                  domain=c.get("domain") or None, path=c.get("path") or "/")
        self._last = 0.0
        self._csrf = None
        self._devices = None

    # plumbing

    def _throttle(self):
        wait = REQUEST_DELAY - (time.time() - self._last)
        if wait > 0:
            time.sleep(wait)
        self._last = time.time()

    def _persist_cookies(self):
        cookies = [{"name": c.name, "value": c.value, "domain": c.domain,
                    "path": c.path, "expires": c.expires} for c in self.http.cookies]
        self.data["cookies"] = cookies
        save_session(self.data)

    def _request(self, method, url, *, expect_json=True, retries=2, **kw):
        kw.setdefault("timeout", 60)
        kw.setdefault("allow_redirects", False)
        attempt = 0
        while True:
            self._throttle()
            r = self.http.request(method, url, **kw)
            if r.status_code in (429, 500, 502, 503, 504) and attempt < retries:
                attempt += 1
                time.sleep(2 ** attempt)
                continue
            break
        if 300 <= r.status_code < 400:
            loc = r.headers.get("location", "")
            if "/ap/" in loc or "signin" in loc or "landing" in loc:
                raise AuthExpired("Amazon asked to sign in again")
            raise CloudError(f"Unexpected redirect to {loc}")
        if r.status_code in (401, 403):
            raise AuthExpired(f"HTTP {r.status_code}")
        if r.status_code >= 400:
            raise CloudError(f"HTTP {r.status_code}: {r.text[:160]}")
        if expect_json and "html" in r.headers.get("content-type", ""):
            raise AuthExpired("Got Amazon's sign-in page instead of data")
        self._persist_cookies()
        return r

    # documents (MYCD)

    def _mycd_page(self):
        return f"https://www.{self.store}/hz/mycd/digital-console/contentlist/pdocs/dateDsc/"

    def _csrf_token(self):
        if self._csrf:
            return self._csrf
        r = self._request("GET", self._mycd_page(), expect_json=False, allow_redirects=True)
        if "/ap/" in r.url:
            raise AuthExpired("Amazon asked to sign in again")
        m = re.search(r"csrfToken\s*=\s*[\"']([^\"']+)[\"']", r.text)
        if not m:
            raise AuthExpired("No csrfToken on the content page (signed out?)")
        self._csrf = m.group(1)
        return self._csrf

    def _ajax(self, payload: dict) -> dict:
        r = self._request(
            "POST", f"https://www.{self.store}/hz/mycd/ajax",
            data={"data": json.dumps(payload), "csrfToken": self._csrf_token(),
                  "clientId": "MYCD_WebService"},
            headers={"X-Requested-With": "XMLHttpRequest",
                     "Referer": self._mycd_page()})
        try:
            obj = _json(r)
        except ValueError:
            raise AuthExpired("Amazon returned a page instead of data")
        if obj.get("success") is False and obj.get("error"):
            raise CloudError(f"Amazon: {obj['error']}")
        return obj

    def list_documents(self, batch=18, limit=2000) -> list[dict]:
        # Amazon drops titles from pages larger than ~20 items; 18 is what
        # the MYCD page itself requests.
        out, start = [], 0
        while start < limit:
            obj = self._ajax({"param": {"OwnershipData": {
                "sortOrder": "DESCENDING", "sortIndex": "DATE", "startIndex": start,
                "batchSize": batch, "contentType": "KindlePDoc",
                "itemStatus": ["Active"], "isExtendedMYK": False}}})
            data = obj.get("OwnershipData") or {}
            if data.get("error"):
                raise CloudError(f"Amazon: {data['error']}")
            for d in data.get("items") or []:
                if not d.get("asin"):
                    continue
                acquired = d.get("acquiredTime") or d.get("acquiredDate")
                if isinstance(acquired, (int, float)):
                    acquired = datetime.fromtimestamp(
                        acquired / 1000 if acquired > 1e11 else acquired).isoformat()
                out.append({"asin": d["asin"],
                            "title": _unescape(d.get("title") or d["asin"]),
                            "category": d.get("category") or "KindlePDoc",
                            "acquired": acquired if isinstance(acquired, str) else None})
            if not data.get("hasMoreItems"):
                break
            start += batch
        return out

    def devices(self) -> list[dict]:
        if self._devices is None:
            obj = self._ajax({"param": {"GetDevices": {}}})
            self._devices = [d for d in (obj.get("GetDevices") or {}).get("devices", [])
                             if d.get("deviceSerialNumber")]
        return self._devices

    # notebooks (Kindle for Web)

    def list_notebooks(self) -> list[dict]:
        r = self._request("GET", NOTES_API, headers={"Accept": "application/json"})
        try:
            obj = _json(r)
        except ValueError:
            raise CloudError("Notebook list is not JSON")
        nodes = obj.get("itemsList", []) if isinstance(obj, dict) else obj
        out = []

        def walk(nodes, path):
            for n in nodes or []:
                title = (n.get("title") or "Untitled").strip() or "Untitled"
                children = n.get("items") or []
                if n.get("type") == "folder":
                    walk(children, path + [title])
                elif n.get("id"):
                    modified = None
                    for k in ("modificationTime", "lastModified", "lastModifiedTime",
                              "updatedTime", "modifiedTime"):
                        if n.get(k) is not None:
                            modified = n[k]
                            break
                    out.append({"id": n["id"], "title": title, "folder": "/".join(path),
                                "modified": _iso(modified), "type": n.get("type")})
                    walk(children, path)
        walk(nodes, [])
        return out

    def open_notebook(self, notebook_id: str) -> dict:
        known = self.data.get("marketplace_id")
        candidates = [known] if known else []
        candidates += [MARKETPLACE_IDS.get(self.store)] + list(MARKETPLACE_IDS.values())
        seen, last = set(), None
        for mp in candidates:
            if not mp or mp in seen:
                continue
            seen.add(mp)
            try:
                r = self._request(
                    "GET", f"{READ_BASE}/openNotebook?notebookId="
                    f"{urllib.parse.quote(notebook_id, safe='')}&marketplaceId={mp}",
                    headers={"Accept": "application/json"}, retries=2 if mp == known else 0)
                obj = _json(r)
            except AuthExpired:
                if mp == known:
                    raise
                last = AuthExpired("openNotebook refused")
                continue
            except (CloudError, ValueError) as e:
                last = e
                continue
            if obj.get("renderingToken"):
                if mp != known:
                    self.data["marketplace_id"] = mp
                    save_session(self.data)
                meta = obj.get("metadata") or {}
                return {"title": meta.get("title", ""),
                        "total_pages": int(meta.get("totalPages") or 0),
                        "modified": _iso(meta.get("modificationTime")),
                        "token": obj["renderingToken"]}
        raise last or CloudError("Could not open the notebook")

    def render_notebook(self, notebook_id: str, width=1240, height=1754, dpi=150,
                        progress=None) -> list[bytes]:
        meta = self.open_notebook(notebook_id)
        token, total = meta["token"], meta["total_pages"]
        pages: dict[int, bytes] = {}
        start, chunk = 0, 3
        while start < total:
            end = min(start + chunk - 1, total - 1)
            if progress:
                progress(start, total)
            try:
                pngs = self._render_range(token, start, end, width, height, dpi)
            except AuthExpired:
                token = self.open_notebook(notebook_id)["token"]  # short-lived token
                pngs = self._render_range(token, start, end, width, height, dpi)
            for k, png in enumerate(pngs[:end - start + 1]):
                pages[start + k] = png
            start = end + 1
        missing = [i for i in range(total) if i not in pages]
        for i in missing:
            got = self._render_range(token, i, i, width, height, dpi)
            if got:
                pages[i] = got[0]
        if len(pages) != total:
            raise CloudError(f"Got {len(pages)} of {total} pages")
        return [pages[i] for i in range(total)]

    def _render_range(self, token, start, end, width, height, dpi) -> list[bytes]:
        r = self._request(
            "GET", f"{READ_BASE}/renderPage?startPage={start}&endPage={end}"
                   f"&width={width}&height={height}&dpi={dpi}",
            headers={TOKEN_HEADER: token}, expect_json=False)
        return _pngs_from_tar(r.content)


# ── helpers ──────────────────────────────────────────────────────────────────

def _json(r):
    """Amazon often omits the charset; the body is UTF-8."""
    return json.loads(r.content.decode("utf-8"))


def _pngs_from_tar(blob: bytes) -> list[bytes]:
    out = []
    with tarfile.open(fileobj=io.BytesIO(blob)) as tf:
        members = sorted((m for m in tf.getmembers() if m.isfile()),
                         key=lambda m: [int(t) if t.isdigit() else t
                                        for t in re.split(r"(\d+)", m.name)])
        for m in members:
            data = tf.extractfile(m).read()
            if data[:8] == b"\x89PNG\r\n\x1a\n":
                out.append(data)
    return out


def pngs_to_pdf(pngs: list[bytes], out_path: Path):
    import fitz
    doc = fitz.open()
    for png in pngs:
        img = fitz.open("png", png)
        rect = img[0].rect
        page = doc.new_page(width=rect.width, height=rect.height)
        page.insert_image(rect, stream=png)
    doc.save(str(out_path), garbage=3, deflate=True)
    doc.close()


def _iso(v):
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)) or (isinstance(v, str) and v.isdigit()):
        v = float(v)
        return datetime.fromtimestamp(v / 1000 if v > 1e11 else v).isoformat()
    return str(v)


def _unescape(s: str) -> str:
    import html
    return html.unescape(s)


# ── CLI ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    import sys

    p = argparse.ArgumentParser(description="Amazon cloud access for Scribe Library")
    p.add_argument("--login", action="store_true", help="Open the Amazon sign-in window")
    p.add_argument("--store", default="amazon.com", choices=STORES)
    p.add_argument("--status", action="store_true")
    p.add_argument("--logout", action="store_true")
    p.add_argument("--list", action="store_true", help="List documents and notebooks")
    args = p.parse_args()
    logging.basicConfig(level=logging.INFO, stream=sys.stderr,
                        format="%(levelname)s: %(message)s")
    try:
        if args.login:
            s = interactive_login(args.store)
            print(json.dumps({"ok": True, "store": s["store"],
                              "notebooks_ok": s["notebooks_ok"]}), flush=True)
        elif args.logout:
            delete_session()
            print(json.dumps({"ok": True}), flush=True)
        elif args.list:
            c = AmazonCloud()
            docs = c.list_documents()
            nbs = c.list_notebooks() if c.data.get("notebooks_ok") else []
            print(json.dumps({"documents": docs, "notebooks": nbs}, indent=1,
                             ensure_ascii=False), flush=True)
        else:
            print(json.dumps(session_status()), flush=True)
    except AuthExpired as e:
        print(json.dumps({"ok": False, "auth": True, "error": str(e)}), flush=True)
        sys.exit(2)
    except Exception as e:
        print(json.dumps({"ok": False, "error": str(e)}), flush=True)
        sys.exit(1)
