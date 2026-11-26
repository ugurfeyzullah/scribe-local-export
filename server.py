#!/usr/bin/env python3
"""
Scribe Library — web UI for exporting Kindle Scribe handwriting.

Browse the annotated documents and handwritten notebooks pulled from a
USB-connected Kindle Scribe, read them in the browser, and download them as
PDFs. Uses the same pipeline as scribe_annotate.py (which remains the CLI for
batch exports); the UI design comes from the original
technolaaji/scribe-library project.

Major sections of this file (in order):
  1. Imports & app setup
  2. Discovery (documents, notebooks, screenshots from the staging mirror)
  3. Conversion pipelines (annotated-document PDF, notebook PDF)
  4. Flask routes (UI + API)
  5. HTML/CSS/JS bundle
  6. CLI

Usage:
    python server.py             # serve whatever is in scribe_staging/
    python server.py --sync      # pull from the device first, then serve
"""

import argparse
import hashlib
import io
import json
import logging
import re
import shutil
import sys
import threading
import uuid
import zipfile
from datetime import datetime
from pathlib import Path

from flask import (Flask, abort, jsonify, render_template_string, request,
                   send_file)

import amazon_cloud
import nbk_to_pdf
import scribe_annotate

app = Flask(__name__)
log = logging.getLogger(__name__)

APP_DIR = Path(__file__).resolve().parent

# The staging mirror shared with the CLI — a copy of the device's storage.
STAGING_ROOT = APP_DIR / "scribe_staging"

# Cache for converted files (PDFs, page JPEGs) — separate from staging so it
# can be blown away without losing synced content. Lives next to the app so
# system temp cleanups don't discard finished conversions.
CACHE_DIR = APP_DIR / "cache" / "web"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)

# Only one device sync and one conversion at a time.
_sync_lock = threading.Lock()
_convert_lock = threading.Lock()

# Progress of the background sync thread, polled by the UI.
_sync_state = {"running": False, "message": "", "current": 0, "total": 0,
               "result": None, "error": None}

# User-assigned display names (notebook UUID / document ID -> name). Kept
# next to the app, not in staging, so a staging wipe doesn't lose them.
NAMES_PATH = APP_DIR / "notebook_names.json"
DOC_NAMES_PATH = APP_DIR / "document_names.json"

# Progress of the conversion currently running (single slot; conversions are
# serialized by _convert_lock). Polled by the UI's loading overlay.
_convert_state = {"id": None, "message": ""}

# Only one bulk ZIP export at a time; its progress, polled by the UI.
_zip_lock = threading.Lock()
_zip_state = {"running": False, "current": 0, "total": 0, "name": "",
             "download_id": None, "error": None}
_zip_results: dict = {}  # download_id -> zip bytes, consumed once by the client


def _load_names(path=NAMES_PATH) -> dict:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _save_names(names: dict, path=NAMES_PATH):
    path = Path(path)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(names, indent=2, ensure_ascii=False,
                              sort_keys=True), encoding="utf-8")
    tmp.replace(path)


# When each document was added to the device (KFX issue_date, i.e. the
# Send-to-Kindle processing date). Reading it costs milliseconds for typical
# documents but seconds for huge ones, so a background thread fills this
# cache lazily; the UI falls back to the modified time until then.
ISSUE_DATES_PATH = CACHE_DIR / "issue_dates.json"
MAX_KFX_MB_FOR_METADATA = 50


def _issue_date_filler():
    import time
    while True:
        try:
            cache = _load_names(ISSUE_DATES_PATH)
            for rec in _document_records():
                kfx = rec["kfx_path"]
                try:
                    mtime = Path(kfx).stat().st_mtime
                except OSError:
                    continue
                entry = cache.get(rec["asin"])
                if entry and entry[0] == mtime:
                    continue

                date = None
                if Path(kfx).stat().st_size <= MAX_KFX_MB_FOR_METADATA * 1024 * 1024:
                    with _convert_lock:
                        try:
                            from kfxlib.yj_book import YJ_Book
                            from kfxlib.message_logging import JobLog, set_logger
                            set_logger(JobLog(logging.getLogger("kfxlib.metadata")))
                            date = YJ_Book(kfx).get_metadata().issue_date
                        except Exception as e:
                            log.debug("issue_date for %s failed: %s", rec["name"], e)
                        finally:
                            set_logger(None)
                cache[rec["asin"]] = [mtime, date]
                _save_names(cache, ISSUE_DATES_PATH)
        except Exception:
            log.exception("issue_date filler pass failed")
        time.sleep(60)


def start_issue_date_filler():
    threading.Thread(target=_issue_date_filler, daemon=True).start()


def _docs_dir() -> Path:
    """Documents tree; falls back to the staging root for flat folders."""
    d = STAGING_ROOT / "documents"
    return d if d.exists() else STAGING_ROOT


def _notebooks_dir() -> Path:
    """Notebook tree; falls back to the staging root for flat folders."""
    d = STAGING_ROOT / ".notebooks"
    return d if d.exists() else STAGING_ROOT


def _screenshots_dir() -> Path:
    return STAGING_ROOT / "screenshots"


def _thumbs_dir() -> Path:
    return _notebooks_dir() / "thumbnails"


# Device presence, refreshed by a background subprocess watcher. Shell COM
# never runs inside the server process (native crashes would kill Flask).
_device_state = {"name": None}


def _device_watcher():
    import subprocess
    import time
    while True:
        try:
            out = subprocess.run(
                [sys.executable, "-X", "utf8", str(APP_DIR / "scribe_mtp.py"),
                 "--detect"],
                capture_output=True, text=True, encoding="utf-8",
                timeout=30, cwd=str(APP_DIR))
            for line in out.stdout.splitlines():
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                if "device" in msg:
                    _device_state["name"] = msg["device"]
        except Exception:
            pass
        time.sleep(10)


def start_device_watcher():
    threading.Thread(target=_device_watcher, daemon=True).start()


# ── Discovery ────────────────────────────────────────────────────────────────

def _stable_id(path: Path) -> str:
    """Short stable hash of a path. Used for URL-safe IDs."""
    return hashlib.md5(str(Path(path).resolve()).encode()).hexdigest()[:12]


# read_annotations over every document on each /api/library call adds up — cache the
# results by (path, mtimes).
_ann_count_cache: dict = {}


def _annotated_pages(yjr_path: Path, pidmap_path) -> int:
    """Number of annotated PAGES in a document.

    A page can hold more than one annotation; the exact page count needs the
    position map, which exists once the document was exported at least once
    (cache/<ASIN>.pidmap.json). Until then the annotation count is returned —
    an upper bound that is almost always identical.
    """
    pidmap = Path(pidmap_path) if pidmap_path else None
    try:
        mtime = (yjr_path.stat().st_mtime, _annotation_db_mtime(yjr_path))
    except OSError:
        return 0
    pidmap_mtime = 0
    if pidmap and pidmap.exists():
        pidmap_mtime = pidmap.stat().st_mtime

    key = str(yjr_path)
    cached = _ann_count_cache.get(key)
    if cached and cached[0] == (mtime, pidmap_mtime):
        return cached[1]

    try:
        entries = scribe_annotate.read_annotations(yjr_path)
        count = len(entries)
        if entries and pidmap_mtime:
            data = json.loads(pidmap.read_text())
            boundaries = data.get("boundaries", data) if isinstance(data, dict) else data
            count = len({scribe_annotate.pid_to_page(pid, boundaries)
                         for pid, _ in entries})
    except Exception as e:
        log.warning("Could not parse %s: %s", yjr_path, e)
        count = 0
    _ann_count_cache[key] = ((mtime, pidmap_mtime), count)
    return count


def _annotation_db_mtime(yjr: Path) -> float:
    """Newest mtime of the device annotation database(s) in the mirror —
    newer firmware keeps the ink page anchors there instead of the .yjr."""
    return max((p.stat().st_mtime
                for p in scribe_annotate.find_annotation_dbs(yjr)), default=0)


def _notebook_mtime(yjr: Path) -> float:
    """mtime of the document's ink notebook (changes on every new stroke)."""
    asin = scribe_annotate.extract_asin(yjr.parent.name)
    nbk = _notebooks_dir() / f"{asin}!!PDOC!!notebook" / "nbk"
    try:
        return nbk.stat().st_mtime
    except OSError:
        return 0


def _doc_cache_key(yjr: Path, kfx_path) -> str:
    """Conversion-cache key: changes whenever the sidecar, annotation DB,
    ink notebook or KFX changes."""
    parts = [str(yjr.stat().st_mtime), str(_annotation_db_mtime(yjr)),
             str(_notebook_mtime(yjr))]
    if kfx_path:
        try:
            parts.append(str(Path(kfx_path).stat().st_mtime))
        except OSError:
            pass
    return "-".join(parts)


def _document_records() -> list[dict]:
    """Raw discovery records (yjr/notebook/kfx paths) from the shared pipeline."""
    if not _docs_dir().exists() or not _notebooks_dir().exists():
        return []
    return scribe_annotate.discover_documents(str(_docs_dir()), str(_notebooks_dir()))


def discover_document_items() -> list[dict]:
    """Annotatable documents in the staging mirror, ready for the UI."""
    items: list[dict] = []
    doc_names = _load_names(DOC_NAMES_PATH)
    issue_dates = _load_names(ISSUE_DATES_PATH)
    for rec in _document_records():
        yjr = Path(rec["yjr"])
        try:
            stat = yjr.stat()
        except OSError:
            continue
        item_id = _stable_id(yjr)
        size_path = Path(rec["kfx_path"]) if rec["kfx_path"] else yjr
        try:
            size_mb = round(size_path.stat().st_size / 1024 / 1024, 2)
        except OSError:
            size_mb = 0
        # Original format is encoded in the device's .sdr folder name
        fmt_m = re.search(r"\.(pdf|epub|mobi|azw3?|docx?|txt|rtf)_[A-F0-9]{32}\.sdr$",
                          yjr.parent.name, re.IGNORECASE)

        # Export state: has it been converted, and is there new ink since?
        doc_cache = CACHE_DIR / item_id
        exported = (doc_cache / "out.pdf").exists()
        stale = False
        if exported:
            try:
                meta = json.loads((doc_cache / "meta.json").read_text())
                stale = meta.get("key") != _doc_cache_key(yjr, rec["kfx_path"])
            except (OSError, json.JSONDecodeError):
                stale = True

        custom = doc_names.get(rec["asin"], "").strip()
        issue_entry = issue_dates.get(rec["asin"])
        items.append({
            "added": issue_entry[1] if issue_entry else None,
            "kind": "document",
            "id": item_id,
            "name": custom or rec["name"],
            "custom_name": bool(custom),
            "asin": rec["asin"],
            "path": str(yjr),
            "annotated_pages": _annotated_pages(yjr, rec["cache_path"]),
            "format": fmt_m.group(1).upper() if fmt_m else "DOC",
            "size_mb": size_mb,
            "modified": datetime.fromtimestamp(stat.st_mtime).isoformat(),
            "has_cover": (doc_cache / "page_0000.jpg").exists(),
            "exported": exported,
            "stale": stale,
            "unsupported": (doc_cache / "unsupported.json").exists(),
        })
    items.sort(key=lambda n: n.get("added") or n["modified"], reverse=True)
    return items


def _cloud_notebook_titles() -> dict:
    """Notebook UUID -> title from the last cloud listing (if signed in)."""
    return {it["notebook_id"]: it["name"] for it in _cloud_items.values()
            if it.get("kind") == "cloud_notebook"}


def discover_notebook_items() -> list[dict]:
    """Standalone handwritten notebooks in the staging mirror."""
    items: list[dict] = []
    base = _notebooks_dir()
    if not base.exists():
        return items
    names = _load_names()
    for entry in sorted(base.iterdir()):
        if not entry.is_dir() or not UUID_RE.match(entry.name):
            continue
        nbk = entry / "nbk"
        if not nbk.exists():
            continue
        try:
            stat = nbk.stat()
        except OSError:
            continue
        thumb = _thumbs_dir() / f"{entry.name}.png"
        # A rename in this app wins; otherwise the notebook's real title from
        # the Amazon account (cloud notebooks share the device's UUIDs).
        custom = names.get(entry.name, "").strip() or _cloud_notebook_titles().get(entry.name, "")
        items.append({
            "kind": "notebook",
            "id": _stable_id(nbk),
            "name": custom or entry.name,
            "custom_name": bool(custom),
            "uuid": entry.name,
            "path": str(nbk),
            "format": "Notebook",
            "size_mb": round(stat.st_size / 1024 / 1024, 2),
            "modified": datetime.fromtimestamp(stat.st_mtime).isoformat(),
            "has_thumbnail": thumb.exists(),
        })
    items.sort(key=lambda n: n["modified"], reverse=True)
    return items


def discover_screenshot_items() -> list[dict]:
    """Device screenshots in the staging mirror."""
    items: list[dict] = []
    base = _screenshots_dir()
    if not base.exists():
        return items
    for img in sorted(base.iterdir()):
        if not img.is_file() or img.suffix.lower() not in (".png", ".jpg", ".jpeg"):
            continue
        try:
            stat = img.stat()
        except OSError:
            continue
        captured = _parse_screenshot_timestamp(img.name) or stat.st_mtime
        items.append({
            "kind": "screenshot",
            "id": _stable_id(img),
            "name": img.name,
            "filename": img.name,
            "path": str(img),
            "size_mb": round(stat.st_size / 1024 / 1024, 2),
            "size_kb": round(stat.st_size / 1024, 1),
            "modified": datetime.fromtimestamp(captured).isoformat(),
            "captured_at": datetime.fromtimestamp(captured).isoformat(),
            "extension": img.suffix.lower(),
        })
    items.sort(key=lambda n: n["modified"], reverse=True)
    return items


def _parse_screenshot_timestamp(filename: str) -> float | None:
    """Parse 'screenshot_2025_04_04T16_06_35+0200.png' style names."""
    m = re.match(
        r"screenshot_(\d{4})[-_](\d{2})[-_](\d{2})T?[-_]?"
        r"(\d{2})[-_](\d{2})[-_](\d{2})",
        filename, re.IGNORECASE,
    )
    if not m:
        return None
    try:
        return datetime(*map(int, m.groups())).timestamp()
    except (ValueError, OverflowError):
        return None


def all_items() -> list[dict]:
    return (discover_document_items()
            + discover_notebook_items()
            + discover_screenshot_items())


def find_item(item_id: str) -> dict | None:
    if item_id.startswith("cl"):
        return _cloud_items.get(item_id)
    for item in all_items():
        if item["id"] == item_id:
            return item
    return None


# ── Conversion pipelines ─────────────────────────────────────────────────────

def _ensure_notebook_pdf(item: dict) -> Path:
    """Convert (or reuse cached) PDF for one notebook. Raises on failure."""
    nb_cache = CACHE_DIR / item["id"]
    nb_cache.mkdir(parents=True, exist_ok=True)
    pdf_path = nb_cache / "out.pdf"
    meta_path = nb_cache / "meta.json"

    src = Path(item["path"])
    try:
        mtime = src.stat().st_mtime
    except OSError as e:
        raise RuntimeError(f"Cannot read notebook file: {e}")

    if pdf_path.exists() and meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text())
            if meta.get("mtime") == mtime and meta.get("ok"):
                return pdf_path
        except Exception:
            pass

    log.info("Converting notebook %s -> %s", src, pdf_path)
    with _convert_lock:
        _convert_state.update(id=item["id"], message="Rendering notebook pages…")
        try:
            result = nbk_to_pdf.convert_or_raise(src, pdf_path)
        except nbk_to_pdf.ConversionError as e:
            lines = [f"[step: {e.step or 'unknown'}] {e}"]
            if e.kfx_errors:
                lines.append("\nkfxlib errors:")
                lines.extend(f"  - {m}" for m in e.kfx_errors[:10])
            raise RuntimeError("\n".join(lines))
        finally:
            _convert_state.update(id=None, message="")

    meta_path.write_text(json.dumps({"mtime": mtime, "ok": True,
                                     "pages": result.get("pages", 0)}))
    return pdf_path


def _ensure_document_pdf(item: dict) -> Path:
    """Produce the annotated PDF for a document (or plain base if unannotated).

    Base PDF comes from the KFX (usually the embedded original, extracted
    losslessly); annotations are overlaid with the scribe_annotate pipeline.
    """
    rec = next((r for r in _document_records()
                if str(r["yjr"]) == item["path"]), None)
    if rec is None:
        raise RuntimeError("Document disappeared from staging — re-sync?")

    doc_cache = CACHE_DIR / item["id"]
    doc_cache.mkdir(parents=True, exist_ok=True)
    pdf_path = doc_cache / "out.pdf"
    meta_path = doc_cache / "meta.json"

    yjr = Path(rec["yjr"])
    cache_key = _doc_cache_key(yjr, rec["kfx_path"])

    if pdf_path.exists() and meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text())
            if meta.get("key") == cache_key and meta.get("ok"):
                return pdf_path
        except Exception:
            pass

    if not rec["kfx_path"]:
        raise RuntimeError(
            "No KFX file for this document in the staging mirror — "
            "run a sync with the device connected.")

    with _convert_lock:
        try:
            base_path = doc_cache / "base.pdf"
            log.info("Extracting base PDF from %s", rec["kfx_path"])
            _convert_state.update(id=item["id"],
                                  message="Extracting the document from KFX…")
            try:
                base_path.write_bytes(
                    scribe_annotate.reconstruct_pdf_from_kfx(rec["kfx_path"]))
            except RuntimeError as e:
                if "not supported" in str(e):
                    # Remember the verdict so the UI can badge this document.
                    (doc_cache / "unsupported.json").write_text(
                        json.dumps({"reason": str(e)}))
                raise
            # A successful extraction clears any previous verdict.
            (doc_cache / "unsupported.json").unlink(missing_ok=True)

            if item.get("annotated_pages"):
                log.info("Overlaying annotations onto %s", item["name"])

                def _progress(done, total):
                    _convert_state.update(
                        id=item["id"],
                        message=f"Overlaying annotations… {done}/{total}")

                scribe_annotate.overlay_annotations(
                    str(base_path), str(yjr), str(rec["notebook"]), str(pdf_path),
                    kfx_path=rec["kfx_path"], cache_path=rec["cache_path"],
                    progress_callback=_progress)
            else:
                shutil.copyfile(base_path, pdf_path)
        finally:
            _convert_state.update(id=None, message="")

    meta_path.write_text(json.dumps({"key": cache_key, "ok": True}))
    return pdf_path


# Documents beyond this page count skip per-page JPEG extraction — the
# browser's native PDF viewer handles them better than 1800 JPEGs would.
MAX_PAGES_FOR_IMAGES = 400


def _pdf_to_page_images(pdf_path: Path, out_dir: Path,
                        embedded_ok: bool = False) -> list[Path]:
    """Extract per-page JPEGs from a PDF for inline viewing in the reader.

    embedded_ok enables the pull-the-single-embedded-image shortcut. That is
    only correct for notebook PDFs (pure image pages); a real document page
    can carry text/vector content ON TOP of a single background image, which
    the shortcut would silently drop.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    cached = sorted(out_dir.glob("page_*.jpg"))
    if cached:
        return cached

    try:
        import fitz
        with fitz.open(str(pdf_path)) as doc:
            if len(doc) > MAX_PAGES_FOR_IMAGES:
                return []
    except Exception:
        pass

    if embedded_ok:
        page_files = _extract_embedded_page_images(pdf_path, out_dir)
        if page_files:
            return page_files
    return _rasterize_pdf_pages(pdf_path, out_dir)


def _extract_embedded_page_images(pdf_path: Path, out_dir: Path) -> list[Path]:
    """For PDFs with one image per page, pull those out directly."""
    try:
        import pypdf
        from io import BytesIO
        from PIL import Image

        reader = pypdf.PdfReader(str(pdf_path))
        page_files: list[Path] = []
        for i, page in enumerate(reader.pages):
            imgs = list(page.images)
            if len(imgs) != 1:
                return []
            try:
                img = Image.open(BytesIO(imgs[0].data)).convert("RGB")
            except Exception as e:
                log.warning("Page %d image decode failed: %s", i, e)
                continue
            page_file = out_dir / f"page_{i:04d}.jpg"
            img.save(page_file, "JPEG", quality=88, optimize=True)
            img.close()
            page_files.append(page_file)
        return page_files
    except Exception as e:
        log.debug("Embedded-image extraction failed: %s", e)
        return []


def _rasterize_pdf_pages(pdf_path: Path, out_dir: Path) -> list[Path]:
    """Rasterize every page of a PDF using PyMuPDF."""
    try:
        import fitz
    except ImportError:
        return []

    page_files: list[Path] = []
    try:
        doc = fitz.open(str(pdf_path))
        zoom = 150 / 72
        matrix = fitz.Matrix(zoom, zoom)
        for i, page in enumerate(doc):
            pix = page.get_pixmap(matrix=matrix, alpha=False)
            page_file = out_dir / f"page_{i:04d}.jpg"
            pix.pil_save(str(page_file), format="JPEG", quality=85, optimize=True)
            page_files.append(page_file)
        doc.close()
    except Exception as e:
        log.error("PDF rasterization failed: %s", e)
        return []

    return page_files


# ── Routes: UI ───────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template_string(HTML_UI)


# ── Routes: discovery & status ───────────────────────────────────────────────

@app.route("/api/library")
def api_library():
    return jsonify({
        "documents": discover_document_items(),
        "notebooks": discover_notebook_items(),
        "screenshots": discover_screenshot_items(),
    })


@app.route("/api/status")
def api_status():
    device = _device_state["name"]
    converter_msg = nbk_to_pdf._check_deps() or ""
    cache_size = 0
    if CACHE_DIR.exists():
        cache_size = sum(
            f.stat().st_size for f in CACHE_DIR.rglob("*") if f.is_file())

    return jsonify({
        "device_connected": bool(device),
        "device_name": device,
        "staging_root": str(STAGING_ROOT),
        "document_count": len(discover_document_items()),
        "notebook_count": len(discover_notebook_items()),
        "screenshot_count": len(discover_screenshot_items()),
        "converter_ready": not converter_msg,
        "converter_message": converter_msg,
        "cache_size_mb": round(cache_size / 1024 / 1024, 2),
    })


# ── Routes: readers ──────────────────────────────────────────────────────────

@app.route("/api/document/<item_id>")
def api_document(item_id):
    item = find_item(item_id)
    if not item or item["kind"] != "document":
        abort(404)
    try:
        pdf_path = _ensure_document_pdf(item)
        page_files = _pdf_to_page_images(pdf_path, CACHE_DIR / item_id)
    except Exception as e:
        log.exception("Document export failed")
        return jsonify({"error": str(e), **item}), 500

    return jsonify({
        **item,
        "pages": [f"/page/{item_id}/{i:04d}" for i in range(len(page_files))],
        "pdf_url": f"/pdf/{item_id}",
        "fallback_pdf": len(page_files) == 0,
    })


@app.route("/api/notebook/<item_id>")
def api_notebook(item_id):
    item = find_item(item_id)
    if not item or item["kind"] != "notebook":
        abort(404)
    try:
        pdf_path = _ensure_notebook_pdf(item)
        page_files = _pdf_to_page_images(pdf_path, CACHE_DIR / item_id,
                                         embedded_ok=True)
    except Exception as e:
        log.exception("Notebook conversion failed")
        return jsonify({"error": str(e), **item}), 500

    return jsonify({
        **item,
        "pages": [f"/page/{item_id}/{i:04d}" for i in range(len(page_files))],
        "pdf_url": f"/pdf/{item_id}",
        "fallback_pdf": len(page_files) == 0,
    })


@app.route("/thumbnail/<item_id>")
def serve_thumbnail(item_id):
    """Device-generated first-page thumbnail for a notebook."""
    item = find_item(item_id)
    if not item or item["kind"] != "notebook":
        abort(404)
    thumb = _thumbs_dir() / f"{item['uuid']}.png"
    if not thumb.exists():
        abort(404)
    return send_file(str(thumb), mimetype="image/png")


@app.route("/cover/<item_id>")
def serve_cover(item_id):
    """Cover tile: notebook thumbnail, or first page of the converted PDF."""
    item = find_item(item_id)
    if not item:
        abort(404)
    if item["kind"] == "notebook":
        thumb = _thumbs_dir() / f"{item['uuid']}.png"
        if thumb.exists():
            return send_file(str(thumb), mimetype="image/png")
    first_page = CACHE_DIR / item_id / "page_0000.jpg"
    if first_page.exists():
        return send_file(str(first_page), mimetype="image/jpeg")
    abort(404)


@app.route("/page/<item_id>/<page_num>")
def serve_page(item_id, page_num):
    page_file = CACHE_DIR / item_id / f"page_{page_num}.jpg"
    if not page_file.exists():
        abort(404)
    return send_file(str(page_file), mimetype="image/jpeg")


@app.route("/pdf/<item_id>")
def serve_pdf(item_id):
    """Serve the converted/annotated PDF inline."""
    pdf_file = CACHE_DIR / item_id / "out.pdf"
    if not pdf_file.exists():
        abort(404)
    item = find_item(item_id)
    name = (item["name"] if item else item_id)
    if not name.lower().endswith(".pdf"):
        name += ".pdf"
    return send_file(str(pdf_file), mimetype="application/pdf",
                     download_name=name)


@app.route("/screenshot/<item_id>")
def serve_screenshot(item_id):
    item = find_item(item_id)
    if not item or item.get("kind") != "screenshot":
        abort(404)
    src = Path(item["path"])
    if not src.exists():
        abort(404)
    mime = "image/png" if src.suffix.lower() == ".png" else "image/jpeg"
    return send_file(str(src), mimetype=mime)


# ── Routes: sync & cache ─────────────────────────────────────────────────────

def _run_sync():
    """Watch the sync subprocess; progress lands in _sync_state for polling.

    The sync runs in a separate process, not a thread: the Windows Shell COM
    layer can crash natively, and process isolation keeps the server alive.
    """
    import subprocess
    try:
        cmd = [sys.executable, "-X", "utf8", str(APP_DIR / "scribe_mtp.py"),
               "--staging", str(STAGING_ROOT),
               "--include-notebooks", "--include-screenshots",
               "--json-progress"]
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL,
                                text=True, encoding="utf-8", cwd=str(APP_DIR))
        for line in proc.stdout:
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if "progress" in msg:
                _sync_state.update(**msg["progress"])
            elif "result" in msg:
                _sync_state["result"] = msg["result"]
            elif "error" in msg:
                _sync_state["error"] = msg["error"]
        proc.wait()
        if proc.returncode != 0 and not _sync_state["error"]:
            _sync_state["error"] = f"Sync process exited with code {proc.returncode}"
    except Exception as e:
        log.exception("Sync failed")
        _sync_state["error"] = str(e)
    finally:
        _sync_state["running"] = False
        _sync_lock.release()


@app.route("/api/sync", methods=["POST"])
def api_sync():
    """Start a background device sync; poll /api/sync_progress for status."""
    if not _sync_lock.acquire(blocking=False):
        return jsonify({"ok": True, "started": False,
                        "note": "A sync is already running"})
    _sync_state.update(running=True, message="Connecting to device…",
                       current=0, total=0, result=None, error=None)
    threading.Thread(target=_run_sync, daemon=True).start()
    return jsonify({"ok": True, "started": True})


@app.route("/api/sync_progress")
def api_sync_progress():
    return jsonify(_sync_state)


@app.route("/api/notebook/<item_id>/rename", methods=["POST"])
def api_rename_notebook(item_id):
    """Set (or clear, with empty name) a notebook's display name."""
    item = find_item(item_id)
    if not item or item["kind"] != "notebook":
        abort(404)
    name = ((request.get_json(silent=True) or {}).get("name") or "").strip()
    names = _load_names()
    if name:
        names[item["uuid"]] = name
    else:
        names.pop(item["uuid"], None)
    _save_names(names)
    return jsonify({"ok": True, "name": name or item["uuid"]})


@app.route("/api/document/<item_id>/rename", methods=["POST"])
def api_rename_document(item_id):
    """Set (or clear, with empty name) a document's display name."""
    item = find_item(item_id)
    if not item or item["kind"] != "document":
        abort(404)
    name = ((request.get_json(silent=True) or {}).get("name") or "").strip()
    # Saving the unchanged device name is the same as having no custom name.
    rec = next((r for r in _document_records()
                if r["asin"] == item["asin"]), None)
    if rec and name == rec["name"]:
        name = ""
    names = _load_names(DOC_NAMES_PATH)
    if name:
        names[item["asin"]] = name
    else:
        names.pop(item["asin"], None)
    _save_names(names, DOC_NAMES_PATH)
    return jsonify({"ok": True, "name": name})


@app.route("/api/convert_progress/<item_id>")
def api_convert_progress(item_id):
    """Progress message of the running conversion, if it is this item's."""
    if _convert_state["id"] == item_id:
        return jsonify({"message": _convert_state["message"]})
    return jsonify({"message": None})


def _run_zip_export(ids, download_id):
    """Background worker: convert each selected item and bundle into a ZIP.

    Progress lands in _zip_state for polling; the finished bytes are
    stashed in _zip_results under download_id for the client to fetch once.
    """
    by_id = {i["id"]: i for i in all_items()}
    errors: list[str] = []
    used_names: set = set()

    def arc_name(base, ext):
        name, n = f"{base}{ext}", 2
        while name.lower() in used_names:
            name, n = f"{base} ({n}){ext}", n + 1
        used_names.add(name.lower())
        return name

    try:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for i, iid in enumerate(ids, 1):
                item = by_id.get(iid)
                if not item:
                    continue
                _zip_state.update(current=i, name=item.get("name", iid))
                try:
                    if item["kind"] == "screenshot":
                        src = Path(item["path"])
                        zf.write(src, arc_name(src.stem, src.suffix))
                    elif item["kind"] == "notebook":
                        zf.write(_ensure_notebook_pdf(item),
                                 arc_name(item["name"], ".pdf"))
                    else:
                        zf.write(_ensure_document_pdf(item),
                                 arc_name(item["name"], ".pdf"))
                except Exception as e:
                    log.exception("ZIP export failed for %s", item.get("name"))
                    errors.append(f"{item.get('name', iid)}: {e}")
            if errors:
                zf.writestr("_export_errors.txt", "\n".join(errors))
        buf.seek(0)
        _zip_results[download_id] = buf.getvalue()
    except Exception as e:
        log.exception("ZIP export failed")
        _zip_state["error"] = str(e)
    finally:
        _zip_state["running"] = False
        _zip_lock.release()


@app.route("/api/download_zip", methods=["POST"])
def api_download_zip():
    """Start a background bulk export; poll /api/zip_progress for status."""
    ids = (request.get_json(silent=True) or {}).get("ids", [])
    if not ids:
        return jsonify({"error": "Nothing selected"}), 400
    if not _zip_lock.acquire(blocking=False):
        return jsonify({"error": "An export is already running"}), 409

    download_id = uuid.uuid4().hex
    _zip_state.update(running=True, current=0, total=len(ids), name="",
                      download_id=download_id, error=None)
    threading.Thread(target=_run_zip_export, args=(ids, download_id),
                     daemon=True).start()
    return jsonify({"ok": True, "download_id": download_id})


@app.route("/api/zip_progress")
def api_zip_progress():
    return jsonify(_zip_state)


@app.route("/api/zip_result/<download_id>")
def api_zip_result(download_id):
    data = _zip_results.pop(download_id, None)
    if data is None:
        abort(404)
    return send_file(io.BytesIO(data), mimetype="application/zip",
                     as_attachment=True, download_name="scribe_export.zip")


@app.route("/api/clear_cache", methods=["POST"])
def clear_cache():
    shutil.rmtree(CACHE_DIR, ignore_errors=True)
    CACHE_DIR.mkdir(exist_ok=True)
    return jsonify({"ok": True})


# ── Routes: Amazon cloud (wireless) ──────────────────────────────────────────
#
# The Kindle phone apps never talk to the device - they read your Amazon
# account, which the Scribe syncs to over Wi-Fi. The Cloud tab does the same
# through amazon_cloud.py: notebooks from Kindle for Web, personal documents
# from "Manage Your Content and Devices". Sign-in happens in a separate
# process (a native window with Amazon's own page).

_cloud_lock = threading.Lock()
_cloud_items: dict = {}      # item id -> item dict (last listing)
_cloud_login = {"running": False, "error": None}
CLOUD_LIST_PATH = CACHE_DIR / "cloud_library.json"


def _cloud_id(kind: str, key: str) -> str:
    return "cl" + hashlib.md5(f"{kind}:{key}".encode()).hexdigest()[:10]


def _cloud_error(e: Exception):
    if isinstance(e, amazon_cloud.AuthExpired):
        return jsonify({"ok": False, "auth": True,
                        "error": "Your Amazon sign-in has expired - sign in again."}), 401
    log.exception("Cloud request failed")
    return jsonify({"ok": False, "error": str(e)}), 502


def _cloud_index(lib: dict):
    _cloud_items.clear()
    for it in lib.get("documents", []) + lib.get("notebooks", []):
        _cloud_items[it["id"]] = it


def _load_cloud_cache():
    try:
        _cloud_index(json.loads(CLOUD_LIST_PATH.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        pass


@app.route("/api/cloud/status")
def api_cloud_status():
    st = amazon_cloud.session_status()
    st["login_running"] = _cloud_login["running"]
    st["login_error"] = _cloud_login["error"]
    st["stores"] = amazon_cloud.STORES
    return jsonify(st)


def _run_cloud_login(store):
    import subprocess
    try:
        out = subprocess.run(
            [sys.executable, "-X", "utf8", str(APP_DIR / "amazon_cloud.py"),
             "--login", "--store", store],
            capture_output=True, text=True, encoding="utf-8", timeout=1200,
            cwd=str(APP_DIR))
        msg = {}
        for line in out.stdout.splitlines():
            try:
                msg = json.loads(line)
            except ValueError:
                pass
        if not msg.get("ok"):
            _cloud_login["error"] = msg.get("error") or "Sign-in did not finish"
    except Exception as e:
        _cloud_login["error"] = str(e)
    finally:
        _cloud_login["running"] = False


@app.route("/api/cloud/login", methods=["POST"])
def api_cloud_login():
    store = (request.get_json(silent=True) or {}).get("store", "amazon.com")
    if store not in amazon_cloud.STORES:
        return jsonify({"ok": False, "error": f"Unknown store {store}"}), 400
    if _cloud_login["running"]:
        return jsonify({"ok": True, "message": "Sign-in window already open"})
    _cloud_login.update(running=True, error=None)
    threading.Thread(target=_run_cloud_login, args=(store,), daemon=True).start()
    return jsonify({"ok": True})


@app.route("/api/cloud/logout", methods=["POST"])
def api_cloud_logout():
    amazon_cloud.delete_session()
    _cloud_items.clear()
    CLOUD_LIST_PATH.unlink(missing_ok=True)
    return jsonify({"ok": True})


@app.route("/api/cloud/library")
def api_cloud_library():
    refresh = request.args.get("refresh") == "1"
    if not refresh and CLOUD_LIST_PATH.exists():
        lib = json.loads(CLOUD_LIST_PATH.read_text(encoding="utf-8"))
        _cloud_index(lib)
        return jsonify({"ok": True, **lib})
    if not amazon_cloud.load_session():
        return jsonify({"ok": False, "auth": True, "error": "Not signed in"}), 401
    try:
        with _cloud_lock:
            c = amazon_cloud.AmazonCloud()
            docs = c.list_documents()
            notebooks = []
            nb_error = None
            try:
                notebooks = c.list_notebooks()
            except Exception as e:     # documents still useful on their own
                nb_error = str(e)
    except Exception as e:
        return _cloud_error(e)
    lib = {
        "fetched_at": datetime.now().isoformat(),
        "notebook_error": nb_error,
        "documents": [{
            "kind": "cloud_document", "id": _cloud_id("doc", d["asin"]),
            "asin": d["asin"], "name": d["title"], "format": "DOC",
            "added": d.get("acquired"), "modified": d.get("acquired"),
            "has_cover": (CACHE_DIR / _cloud_id("doc", d["asin"]) / "page_0000.jpg").exists(),
        } for d in docs],
        "notebooks": [{
            "kind": "cloud_notebook", "id": _cloud_id("nb", n["id"]),
            "notebook_id": n["id"], "name": n["title"], "folder": n.get("folder", ""),
            "format": "Notebook", "modified": n.get("modified"),
            "has_cover": (CACHE_DIR / _cloud_id("nb", n["id"]) / "page_0000.jpg").exists(),
        } for n in notebooks],
    }
    CLOUD_LIST_PATH.write_text(json.dumps(lib, ensure_ascii=False), encoding="utf-8")
    _cloud_index(lib)
    return jsonify({"ok": True, **lib})


def _ensure_cloud_notebook_pdf(item: dict) -> Path:
    """Render a cloud notebook through Kindle for Web into out.pdf (cached
    until the notebook changes)."""
    d = CACHE_DIR / item["id"]
    d.mkdir(parents=True, exist_ok=True)
    pdf = d / "out.pdf"
    meta_path = d / "meta.json"
    stamp = item.get("modified") or ""
    if pdf.exists() and meta_path.exists():
        try:
            if json.loads(meta_path.read_text()).get("stamp") == stamp:
                return pdf
        except ValueError:
            pass
    with _cloud_lock:
        c = amazon_cloud.AmazonCloud()
        _convert_state.update(id=item["id"], message="Downloading notebook pages…")
        try:
            pngs = c.render_notebook(
                item["notebook_id"],
                progress=lambda i, n: _convert_state.update(
                    id=item["id"], message=f"Downloading pages… {i}/{n}"))
        finally:
            _convert_state.update(id=None, message="")
    amazon_cloud.pngs_to_pdf(pngs, pdf)
    meta_path.write_text(json.dumps({"stamp": stamp}))
    return pdf


@app.route("/api/cloud/item/<item_id>")
def api_cloud_item(item_id):
    item = _cloud_items.get(item_id)
    if not item:
        abort(404)
    if item["kind"] == "cloud_document":
        # Amazon's website does not hand out personal-document files (its own
        # "download" actions answer "not available" for them), so a cloud
        # document opens from the USB-synced copy when there is one - with
        # your ink overlaid.
        local = next((d for d in discover_document_items()
                      if d["asin"] == item["asin"]), None)
        if not local:
            return jsonify({**item, "error":
                "Amazon doesn't let other apps download your sent documents - "
                "only your Kindle and Amazon's apps receive the file. Sync the "
                "Kindle over USB once and this document opens here, with your "
                "handwriting."}), 409
        return api_document(local["id"])
    try:
        pdf_path = _ensure_cloud_notebook_pdf(item)
        pages = _pdf_to_page_images(pdf_path, CACHE_DIR / item_id, embedded_ok=True)
    except amazon_cloud.AuthExpired as e:
        return _cloud_error(e)
    except Exception as e:
        log.exception("Cloud notebook failed")
        return jsonify({"error": str(e), **item}), 500
    return jsonify({
        **item,
        "pages": [f"/page/{item_id}/{i:04d}" for i in range(len(pages))],
        "pdf_url": f"/pdf/{item_id}",
        "fallback_pdf": len(pages) == 0,
    })


# ── Routes: upload ───────────────────────────────────────────────────────────
#
# Two ways to put a document on the Kindle:
#   "stk" - hand the files to Amazon's Send to Kindle for PC, exactly like
#           Explorer's right-click "Send to Kindle" does (StkSendToHandler.exe
#           <files>). The official app uploads them with its own sign-in;
#           Amazon converts them and they arrive on every chosen device with
#           full Scribe support (writing on PDFs, export). This app never
#           touches Amazon credentials.
#   "usb" - copy the files over USB into documents/Uploads on the device.
#           Instant and offline, but the file stays a sideloaded document.

UPLOAD_DIR = APP_DIR / "uploads"
UPLOAD_EXTENSIONS = {".pdf", ".epub", ".txt", ".doc", ".docx", ".rtf", ".htm",
                     ".html", ".jpg", ".jpeg", ".png", ".gif", ".bmp", ".mobi",
                     ".azw3", ".kfx", ".prc"}
# USB sideloading only makes sense for formats the device opens natively.
USB_EXTENSIONS = {".pdf", ".epub", ".txt", ".mobi", ".azw3", ".kfx", ".prc"}
STK_HANDLER_NAME = "StkSendToHandler.exe"
_stk_override: dict = {"path": None}


def find_stk_handler() -> Path | None:
    """Locate Send to Kindle for PC's shell hand-off program."""
    import os
    candidates = []
    if _stk_override["path"]:
        candidates.append(Path(_stk_override["path"]))
    if os.environ.get("STK_HANDLER"):
        candidates.append(Path(os.environ["STK_HANDLER"]))
    try:
        import winreg
        # The installer writes an uninstall entry that reveals the install
        # directory.
        for root, key in ((winreg.HKEY_LOCAL_MACHINE,
                           r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\SendToKindle"),
                          (winreg.HKEY_LOCAL_MACHINE,
                           r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\SendToKindle"),
                          (winreg.HKEY_CURRENT_USER,
                           r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\SendToKindle")):
            try:
                with winreg.OpenKey(root, key) as k:
                    for value in ("InstallLocation", "UninstallString", "DisplayIcon"):
                        try:
                            v = str(winreg.QueryValueEx(k, value)[0]).strip('"')
                        except OSError:
                            continue
                        p = Path(v)
                        candidates.append((p if p.is_dir() else p.parent) / STK_HANDLER_NAME)
            except OSError:
                pass
    except ImportError:
        pass
    for base in (os.environ.get("ProgramFiles(x86)"), os.environ.get("ProgramFiles"),
                 "D:/Apps", "D:/", "C:/"):
        if base:
            candidates.append(Path(base) / "Amazon" / "SendToKindle" / STK_HANDLER_NAME)
    for c in candidates:
        if c.is_file():
            return c
    return None


def _safe_upload_name(name: str) -> str:
    """Keep the user's file name (incl. non-ASCII) but strip path parts and
    characters Windows/MTP reject."""
    name = Path(name.replace("\\", "/")).name
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .")
    return name or "document"


@app.route("/api/upload_targets")
def api_upload_targets():
    stk = find_stk_handler()
    return jsonify({
        "stk_available": bool(stk),
        "stk_path": str(stk) if stk else None,
        "device_connected": bool(_device_state["name"]),
        "device_name": _device_state["name"],
        "extensions": sorted(UPLOAD_EXTENSIONS),
        "usb_extensions": sorted(USB_EXTENSIONS),
    })


@app.route("/api/upload", methods=["POST"])
def api_upload():
    import subprocess
    method = request.form.get("method", "stk")
    files = request.files.getlist("files")
    if not files:
        return jsonify({"ok": False, "error": "No files received"}), 400
    if method not in ("stk", "usb"):
        return jsonify({"ok": False, "error": f"Unknown method {method!r}"}), 400

    allowed = USB_EXTENSIONS if method == "usb" else UPLOAD_EXTENSIONS
    batch_dir = UPLOAD_DIR / (datetime.now().strftime("%Y%m%d-%H%M%S-")
                              + uuid.uuid4().hex[:6])
    saved, rejected = [], []
    for f in files:
        name = _safe_upload_name(f.filename or "")
        if Path(name).suffix.lower() not in allowed:
            rejected.append({"file": name, "ok": False,
                             "error": "file type not supported for this method"})
            continue
        batch_dir.mkdir(parents=True, exist_ok=True)
        dest = batch_dir / name
        f.save(str(dest))
        saved.append(dest)
    if not saved:
        return jsonify({"ok": False, "results": rejected,
                        "error": "None of the files can be sent this way"}), 400

    if method == "stk":
        handler = find_stk_handler()
        if not handler:
            return jsonify({"ok": False, "results": rejected, "error":
                            "Send to Kindle for PC was not found. Install it from "
                            "amazon.com/sendtokindle, or start the server with "
                            "--stk-handler <path to StkSendToHandler.exe>."}), 400
        # Same hand-off as Explorer's "Send to Kindle" context-menu entry.
        subprocess.Popen([str(handler)] + [str(p) for p in saved],
                         cwd=str(handler.parent))
        results = [{"file": p.name, "ok": True, "error": None} for p in saved]
        return jsonify({"ok": True, "method": "stk", "results": results + rejected,
                        "message": "Opened Send to Kindle. Choose your device and "
                                   "click Send in its window."})

    # USB: Shell COM runs in a subprocess, like the sync (native crashes in
    # COM must not take the server down); one MTP job at a time.
    if not _sync_lock.acquire(blocking=False):
        return jsonify({"ok": False, "error": "A device sync is running - try again "
                                              "when it has finished."}), 409
    try:
        out = subprocess.run(
            [sys.executable, "-X", "utf8", str(APP_DIR / "scribe_mtp.py"),
             "--upload"] + [str(p) for p in saved],
            capture_output=True, text=True, encoding="utf-8",
            timeout=1800, cwd=str(APP_DIR))
    except subprocess.TimeoutExpired:
        return jsonify({"ok": False, "error": "USB copy timed out"}), 504
    finally:
        _sync_lock.release()
    msg = {}
    for line in out.stdout.splitlines():
        try:
            msg = json.loads(line)
        except ValueError:
            continue
    if "error" in msg:
        return jsonify({"ok": False, "results": rejected, "error": msg["error"]}), 502
    results = msg.get("uploaded", [])
    return jsonify({"ok": bool(results) and all(r["ok"] for r in results),
                    "method": "usb", "results": results + rejected,
                    "message": "Copied to documents/Uploads on the Kindle."})


# ── HTML UI ──────────────────────────────────────────────────────────────────

HTML_UI = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Scribe Library</title>
<style>
  @import url('https://fonts.googleapis.com/css2?family=Playfair+Display:ital,wght@0,400;0,600;1,400&family=DM+Mono:wght@300;400&family=DM+Sans:wght@300;400;500&display=swap');

  :root {
    --ink: #1a1410;
    --paper: #f5f0e8;
    --paper-dark: #ede7d7;
    --paper-border: #d4c9b0;
    --accent: #8b5e3c;
    --accent-light: #c4956a;
    --accent-pale: #f0e4d4;
    --text: #2d2318;
    --text-muted: #7a6a5a;
    --shadow: rgba(26,20,16,0.15);
    --page-bg: #fdfaf5;
    --book-accent: #4a6fa5;
    --book-accent-pale: #e0e8f5;
    --notebook-accent: #8b5e3c;
    --notebook-accent-pale: #f0e4d4;
  }

  * { box-sizing: border-box; margin: 0; padding: 0; }

  body {
    background: var(--paper);
    color: var(--text);
    font-family: 'DM Sans', sans-serif;
    font-weight: 300;
    min-height: 100vh;
    background-image: repeating-linear-gradient(
      0deg, transparent, transparent 27px, rgba(180,160,130,0.12) 28px);
  }

  .shell {
    display: grid;
    grid-template-columns: 320px 1fr;
    grid-template-rows: 60px 1fr;
    height: 100vh;
    overflow: hidden;
  }

  .topbar {
    grid-column: 1 / -1;
    background: var(--ink);
    display: flex;
    align-items: center;
    padding: 0 24px;
    gap: 12px;
    box-shadow: 0 2px 12px rgba(0,0,0,0.3);
    z-index: 100;
  }

  .logo {
    font-family: 'Playfair Display', serif;
    font-size: 1.35rem;
    color: var(--paper);
    letter-spacing: 0.02em;
    display: flex;
    align-items: center;
    gap: 10px;
  }
  .logo-icon { width: 22px; height: 22px; opacity: 0.85; }

  .topbar-status {
    margin-left: auto;
    font-family: 'DM Mono', monospace;
    font-size: 0.7rem;
    color: rgba(255,255,255,0.45);
    display: flex;
    align-items: center;
  }

  .btn-icon, .btn-sync, .btn-upload {
    background: none;
    border: 1px solid rgba(255,255,255,0.15);
    color: rgba(255,255,255,0.6);
    border-radius: 6px;
    padding: 6px 12px;
    font-family: 'DM Mono', monospace;
    font-size: 0.7rem;
    cursor: pointer;
    transition: all 0.2s;
    letter-spacing: 0.05em;
  }
  .btn-icon:hover { background: rgba(255,255,255,0.1); color: white; }

  .btn-sync {
    background: var(--accent);
    border: none;
    color: white;
    font-size: 0.72rem;
    padding: 7px 14px;
    display: flex;
    align-items: center;
    gap: 6px;
  }
  .btn-sync:hover { background: var(--accent-light); }
  .btn-sync:disabled { opacity: 0.5; cursor: not-allowed; }

  .btn-upload {
    background: var(--book-accent);
    border: none;
    color: white;
    font-size: 0.72rem;
    padding: 7px 14px;
    display: flex;
    align-items: center;
    gap: 6px;
  }
  .btn-upload:hover { background: #5a7fb5; }

  #sync-drawer {
    position: fixed;
    top: 68px;
    right: 16px;
    width: 400px;
    z-index: 400;
    background: #16120e;
    border: 1px solid var(--accent);
    border-radius: 10px;
    box-shadow: 0 8px 32px rgba(0,0,0,0.45);
    padding: 12px 16px;
    font-family: 'DM Mono', monospace;
    font-size: 0.72rem;
    color: #aaa;
    height: 140px;
    resize: both;
    overflow: auto;
    min-width: 320px;
    min-height: 90px;
    max-width: 90vw;
    max-height: 85vh;
    line-height: 1.6;
    white-space: pre-wrap;
  }
  #sync-drawer .ok   { color: #7ec87e; }
  #sync-drawer .err  { color: #e07e7e; }
  #sync-drawer .info { color: #7eaee0; }
  .sync-close {
    float: right;
    cursor: pointer;
    color: #666;
    padding: 0 2px;
  }
  .sync-close:hover { color: #fff; }
  .sync-progress-track {
    height: 4px;
    background: #333;
    border-radius: 2px;
    margin: 8px 0 4px;
    overflow: hidden;
  }
  .sync-progress-fill {
    height: 100%;
    width: 0%;
    background: var(--accent);
    transition: width 0.4s;
  }
  #sync-progress-msg {
    color: #7a7a7a;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .device-dot {
    width: 7px; height: 7px;
    border-radius: 50%;
    display: inline-block;
    margin-right: 6px;
    background: #555;
    transition: background 0.5s;
  }
  .device-dot.connected { background: #7ec87e; box-shadow: 0 0 6px #7ec87e88; }

  .sidebar {
    background: var(--paper-dark);
    border-right: 1px solid var(--paper-border);
    overflow: hidden;
    display: flex;
    flex-direction: column;
  }

  .tabs {
    display: flex;
    border-bottom: 1px solid var(--paper-border);
    background: var(--paper);
  }
  .tab {
    flex: 1;
    min-width: 0;
    padding: 12px 4px;
    text-align: center;
    cursor: pointer;
    font-family: 'DM Mono', monospace;
    font-size: 0.66rem;
    letter-spacing: 0.04em;
    text-transform: uppercase;
    color: var(--text-muted);
    border-bottom: 2px solid transparent;
    transition: all 0.15s;
    user-select: none;
    position: relative;
  }
  .tab:hover { color: var(--text); }
  .tab.active {
    color: var(--text);
    border-bottom-color: var(--accent);
    background: var(--paper-dark);
  }
  .tab.active[data-tab="documents"] { border-bottom-color: var(--book-accent); }
  .tab-count {
    background: rgba(0,0,0,0.07);
    font-size: 0.6rem;
    padding: 1px 6px;
    border-radius: 8px;
    margin-left: 4px;
  }

  .sidebar-actions {
    padding: 12px 16px 8px;
    border-bottom: 1px solid var(--paper-border);
    display: flex;
    gap: 8px;
    flex-direction: column;
  }
  .search-box {
    width: 100%;
    padding: 7px 12px;
    background: var(--paper);
    border: 1px solid var(--paper-border);
    border-radius: 6px;
    font-family: 'DM Sans', sans-serif;
    font-size: 0.78rem;
    color: var(--text);
    outline: none;
  }
  .search-box:focus { border-color: var(--accent-light); }

  .filter-chips {
    display: flex;
    gap: 6px;
    flex-wrap: wrap;
  }
  .chip {
    font-family: 'DM Mono', monospace;
    font-size: 0.62rem;
    padding: 3px 8px;
    background: var(--paper);
    border: 1px solid var(--paper-border);
    border-radius: 12px;
    cursor: pointer;
    color: var(--text-muted);
    user-select: none;
    transition: all 0.15s;
  }
  .chip:hover { background: var(--accent-pale); }
  .chip.active {
    background: var(--accent);
    border-color: var(--accent);
    color: white;
  }

  .item-list {
    flex: 1;
    overflow-y: auto;
    padding: 8px;
  }

  .nb-card {
    padding: 12px 14px;
    margin-bottom: 4px;
    border-radius: 8px;
    cursor: pointer;
    transition: all 0.15s;
    border: 1px solid transparent;
    position: relative;
    display: grid;
    grid-template-columns: 44px 1fr;
    gap: 10px;
    align-items: center;
  }
  .nb-card:hover { background: var(--paper); border-color: var(--paper-border); }
  .nb-card.active { background: var(--accent-pale); border-color: var(--accent-light); }
  .nb-card[data-kind="book"].active { background: var(--book-accent-pale); border-color: var(--book-accent); }
  .nb-card.active::before {
    content: '';
    position: absolute;
    left: 0; top: 20%; bottom: 20%;
    width: 3px;
    background: var(--accent);
    border-radius: 0 2px 2px 0;
  }
  .nb-card[data-kind="book"].active::before { background: var(--book-accent); }

  .nb-thumb {
    width: 44px; height: 56px;
    object-fit: cover;
    border-radius: 3px;
    background: white;
    box-shadow: 0 1px 3px var(--shadow);
  }
  .nb-thumb-placeholder {
    width: 44px; height: 56px;
    border-radius: 3px;
    display: flex;
    align-items: center;
    justify-content: center;
    color: var(--text-muted);
    font-size: 0.55rem;
    font-family: 'DM Mono', monospace;
    text-align: center;
    padding: 4px;
    line-height: 1.1;
  }
  .nb-thumb-placeholder.notebook {
    background: var(--paper);
    border: 1px dashed var(--paper-border);
  }
  .nb-thumb-placeholder.book {
    background: linear-gradient(135deg, var(--book-accent), #6989b8);
    color: white;
    font-weight: 500;
    border: none;
  }

  .nb-info { min-width: 0; }
  .nb-name {
    font-size: 0.82rem;
    color: var(--text);
    margin-bottom: 4px;
    line-height: 1.3;
    word-break: break-word;
    display: -webkit-box;
    -webkit-line-clamp: 2;
    -webkit-box-orient: vertical;
    overflow: hidden;
  }
  .nb-author {
    font-size: 0.68rem;
    color: var(--text-muted);
    font-style: italic;
    margin-bottom: 3px;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .nb-meta {
    font-family: 'DM Mono', monospace;
    font-size: 0.62rem;
    color: var(--text-muted);
    display: flex;
    gap: 8px;
  }

  .nb-badge {
    display: inline-block;
    font-family: 'DM Mono', monospace;
    font-size: 0.58rem;
    padding: 1px 5px;
    border-radius: 3px;
    color: white;
    margin-bottom: 4px;
    letter-spacing: 0.04em;
  }
  .nb-badge.scribe { background: var(--notebook-accent); }
  .nb-badge.fmt { background: #6b7280; }
  .nb-badge.annotated { background: var(--book-accent); }
  .nb-badge.updated { background: #a07840; }
  .nb-badge.unsupported { background: #9b9b9b; }
  .nb-card.unsupported-card { opacity: 0.55; }

  /* Multi-select checkboxes + selection bar */
  .sel-box {
    position: absolute;
    top: 8px;
    right: 8px;
    width: 15px;
    height: 15px;
    accent-color: var(--accent);
    opacity: 0;
    transition: opacity 0.12s;
    cursor: pointer;
    z-index: 2;
  }
  .nb-card:hover .sel-box, .sel-box:checked { opacity: 1; }
  .card-rename {
    position: absolute;
    top: 28px;
    right: 7px;
    font-size: 0.8rem;
    color: var(--text-muted);
    opacity: 0;
    transition: opacity 0.12s;
    cursor: pointer;
    z-index: 2;
  }
  .nb-card:hover .card-rename { opacity: 1; }
  .card-rename:hover { color: var(--accent); }
  .selection-bar {
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 8px 16px;
    border-bottom: 1px solid var(--paper-border);
    background: var(--accent-pale);
    font-size: 0.75rem;
    color: var(--text);
  }
  .selection-bar button {
    font-family: 'DM Mono', monospace;
    font-size: 0.68rem;
    padding: 3px 10px;
    border-radius: 5px;
    border: 1px solid var(--paper-border);
    background: var(--paper);
    color: var(--text);
    cursor: pointer;
  }
  .selection-bar button:hover { border-color: var(--accent); }
  .selection-bar button.primary { background: var(--accent); color: white; border-color: var(--accent); }

  .empty-state {
    padding: 32px 20px;
    text-align: center;
    color: var(--text-muted);
    font-size: 0.82rem;
    line-height: 1.6;
  }

  /* Reader pane */
  .reader {
    display: flex;
    flex-direction: column;
    overflow: hidden;
    background: var(--page-bg);
  }
  .reader-toolbar {
    background: var(--paper-dark);
    border-bottom: 1px solid var(--paper-border);
    padding: 10px 20px;
    display: flex;
    align-items: center;
    gap: 12px;
    flex-shrink: 0;
  }
  .reader-title {
    font-family: 'Playfair Display', serif;
    font-size: 0.95rem;
    color: var(--text);
    flex: 1;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .reader-title em {
    font-style: italic;
    color: var(--text-muted);
    font-size: 0.85rem;
  }

  .page-controls { display: flex; align-items: center; gap: 8px; }
  .btn-nav {
    width: 32px; height: 32px;
    display: flex; align-items: center; justify-content: center;
    background: var(--paper);
    border: 1px solid var(--paper-border);
    border-radius: 6px;
    cursor: pointer;
    color: var(--text);
    font-size: 1rem;
    user-select: none;
    transition: all 0.15s;
  }
  .btn-nav:hover:not(:disabled) {
    background: var(--accent-pale);
    border-color: var(--accent-light);
    color: var(--accent);
  }
  .btn-nav:disabled { opacity: 0.3; cursor: default; }
  .page-indicator {
    font-family: 'DM Mono', monospace;
    font-size: 0.72rem;
    color: var(--text-muted);
    min-width: 60px;
    text-align: center;
  }

  .zoom-controls {
    display: flex;
    align-items: center;
    gap: 6px;
    border-left: 1px solid var(--paper-border);
    padding-left: 12px;
  }
  .zoom-label {
    font-family: 'DM Mono', monospace;
    font-size: 0.68rem;
    color: var(--text-muted);
    min-width: 38px;
    text-align: center;
  }

  .page-viewport {
    flex: 1;
    overflow: auto;
    display: flex;
    align-items: flex-start;
    justify-content: center;
    padding: 32px;
    background: radial-gradient(ellipse at 50% 0%, rgba(180,160,130,0.08) 0%, transparent 70%), var(--page-bg);
    position: relative;
  }

  .page-wrapper {
    position: relative;
    transform-origin: top center;
    transition: transform 0.2s ease;
  }

  .page-shadow {
    box-shadow: 0 4px 16px var(--shadow), 0 1px 4px rgba(0,0,0,0.1), inset 0 0 0 1px rgba(0,0,0,0.06);
    border-radius: 2px;
  }

  .single-page {
    /* Container that JS sizes to fit the viewport; page-img fills it. */
    display: block;
    margin: 0 auto;
  }

  .page-img {
    display: block;
    width: 100%;
    height: 100%;
    object-fit: contain;
    border-radius: 2px;
    background: white;
  }

  .pdf-iframe {
    width: 100%;
    height: 100%;
    border: none;
    background: white;
  }

  .loading-overlay {
    position: absolute;
    inset: 0;
    background: rgba(245,240,232,0.85);
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    gap: 16px;
    z-index: 10;
    backdrop-filter: blur(4px);
  }
  .spinner {
    width: 36px; height: 36px;
    border: 3px solid var(--paper-border);
    border-top-color: var(--accent);
    border-radius: 50%;
    animation: spin 0.8s linear infinite;
  }
  @keyframes spin { to { transform: rotate(360deg); } }
  .loading-text {
    font-family: 'DM Mono', monospace;
    font-size: 0.75rem;
    color: var(--text-muted);
  }
  .loading-sub {
    font-family: 'DM Sans', sans-serif;
    font-size: 0.7rem;
    color: var(--text-muted);
    opacity: 0.7;
    max-width: 320px;
    text-align: center;
    line-height: 1.5;
  }

  .welcome {
    flex: 1;
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    padding: 48px;
    text-align: center;
    gap: 16px;
  }
  .welcome-icon { width: 72px; height: 72px; opacity: 0.2; }
  .welcome h2 {
    font-family: 'Playfair Display', serif;
    font-size: 1.6rem;
    color: var(--text);
    font-weight: 400;
  }
  .welcome p {
    color: var(--text-muted);
    font-size: 0.85rem;
    line-height: 1.7;
    max-width: 380px;
  }

  .thumb-strip {
    display: flex;
    gap: 8px;
    padding: 10px 16px;
    background: var(--paper-dark);
    border-top: 1px solid var(--paper-border);
    overflow-x: auto;
    flex-shrink: 0;
    scrollbar-width: thin;
    scrollbar-color: var(--paper-border) transparent;
  }
  .thumb {
    flex-shrink: 0;
    width: 52px;
    height: 68px;
    object-fit: cover;
    border-radius: 3px;
    border: 2px solid transparent;
    cursor: pointer;
    opacity: 0.6;
    transition: all 0.15s;
    background: white;
  }
  .thumb:hover { opacity: 0.9; transform: translateY(-2px); }
  .thumb.active {
    border-color: var(--accent);
    opacity: 1;
    box-shadow: 0 2px 8px var(--shadow);
  }

  .error-box, .info-box {
    margin: 32px auto;
    max-width: 480px;
    padding: 24px;
    border-radius: 8px;
    font-size: 0.82rem;
    line-height: 1.6;
  }
  .error-box {
    background: #fff5f5;
    border: 1px solid #f5c6c6;
    color: #8b3a3a;
  }
  .info-box {
    background: var(--paper-dark);
    border: 1px solid var(--paper-border);
    color: var(--text);
  }
  .error-box strong, .info-box strong { display: block; margin-bottom: 8px; font-size: 0.9rem; }

  .download-link {
    display: inline-block;
    margin-top: 12px;
    padding: 8px 16px;
    background: var(--book-accent);
    color: white;
    text-decoration: none;
    border-radius: 6px;
    font-family: 'DM Mono', monospace;
    font-size: 0.75rem;
  }
  .download-link:hover { background: #5a7fb5; }

  .warn-banner {
    grid-column: 1 / -1;
    background: #fef5e0;
    border-bottom: 1px solid #e5c87a;
    color: #6e4f1c;
    padding: 8px 24px;
    font-size: 0.78rem;
    font-family: 'DM Mono', monospace;
  }

  /* Upload modal */
  .modal-bg {
    position: fixed;
    inset: 0;
    background: rgba(0,0,0,0.5);
    display: none;
    align-items: center;
    justify-content: center;
    z-index: 1000;
  }
  .modal-bg.show { display: flex; }
  .modal {
    background: var(--paper);
    border-radius: 12px;
    width: 480px;
    max-width: 90vw;
    padding: 24px;
    box-shadow: 0 8px 40px rgba(0,0,0,0.3);
  }
  .modal h3 {
    font-family: 'Playfair Display', serif;
    font-size: 1.2rem;
    margin-bottom: 4px;
  }
  .modal p { font-size: 0.82rem; color: var(--text-muted); margin-bottom: 16px; line-height: 1.5; }
  .drop-zone {
    border: 2px dashed var(--paper-border);
    border-radius: 8px;
    padding: 32px 24px;
    text-align: center;
    background: white;
    transition: all 0.15s;
    cursor: pointer;
  }
  .drop-zone:hover, .drop-zone.dragover {
    border-color: var(--book-accent);
    background: var(--book-accent-pale);
  }
  .drop-zone-text {
    font-family: 'DM Mono', monospace;
    font-size: 0.78rem;
    color: var(--text-muted);
    margin-bottom: 6px;
  }
  .drop-zone-hint {
    font-size: 0.72rem;
    color: var(--text-muted);
    opacity: 0.6;
  }
  .modal-actions {
    margin-top: 20px;
    display: flex;
    justify-content: flex-end;
    gap: 8px;
  }
  .btn-modal {
    padding: 8px 16px;
    border-radius: 6px;
    font-family: 'DM Mono', monospace;
    font-size: 0.72rem;
    cursor: pointer;
    border: 1px solid var(--paper-border);
    background: var(--paper);
    color: var(--text);
  }
  .btn-modal.primary {
    background: var(--book-accent);
    border-color: var(--book-accent);
    color: white;
  }
  .btn-modal:disabled { opacity: 0.5; cursor: not-allowed; }
  .tab.active[data-tab="cloud"] { border-bottom-color: var(--book-accent); }
  .cloud-panel { padding: 14px 12px; font-size: 0.8rem; line-height: 1.5; color: var(--text-muted); }
  .cloud-panel h4 { font-family: 'Playfair Display', serif; color: var(--text); font-size: 1rem; margin-bottom: 6px; }
  .cloud-panel select { width: 100%; margin: 10px 0; padding: 6px; border-radius: 6px;
    border: 1px solid var(--paper-border); background: white; font-family: 'DM Mono', monospace; font-size: 0.75rem; }
  .cloud-panel .btn-modal { width: 100%; }
  .cloud-bar { display: flex; gap: 6px; align-items: center; padding: 6px 12px 8px;
    font-family: 'DM Mono', monospace; font-size: 0.65rem; color: var(--text-muted); }
  .cloud-bar span { flex: 1; }
  .cloud-bar button { font-family: 'DM Mono', monospace; font-size: 0.65rem; cursor: pointer;
    border: 1px solid var(--paper-border); background: var(--paper); border-radius: 5px; padding: 3px 7px; }
  .cloud-section { padding: 8px 12px 2px; font-family: 'DM Mono', monospace; font-size: 0.62rem;
    text-transform: uppercase; letter-spacing: 0.08em; color: var(--text-muted); }
  .nb-badge.cloud { background: #e3ecf7; color: #33557f; }
  .upload-methods { margin-top: 16px; display: flex; flex-direction: column; gap: 10px; }
  .upload-method { display: flex; gap: 10px; align-items: flex-start; cursor: pointer;
    font-size: 0.82rem; line-height: 1.4; }
  .upload-method input { margin-top: 3px; }
  .upload-method small { display: block; color: var(--text-muted); font-size: 0.74rem; }
  .upload-method.disabled { opacity: 0.5; cursor: not-allowed; }
  .upload-status .ok { color: #3a7d44; }
  .upload-status .err { color: #b33; }
  .upload-status {
    margin-top: 16px;
    font-family: 'DM Mono', monospace;
    font-size: 0.7rem;
    color: var(--text-muted);
    max-height: 120px;
    overflow-y: auto;
    line-height: 1.5;
    white-space: pre-wrap;
  }

  @media (max-width: 700px) {
    .shell { grid-template-columns: 1fr; grid-template-rows: 60px auto 1fr; }
    .sidebar { max-height: 280px; }
  }

  /* ── Zen mode ──────────────────────────────────────────────────────────── */
  /* Hide sidebar, topbar, thumb-strip; reveal a floating bottom toolbar */
  body.zen-mode .topbar,
  body.zen-mode .sidebar,
  body.zen-mode .thumb-strip,
  body.zen-mode .reader-toolbar {
    display: none !important;
  }
  body.zen-mode .shell {
    grid-template-columns: 1fr;
    grid-template-rows: 1fr;
  }
  body.zen-mode .reader { grid-column: 1; grid-row: 1; }
  body.zen-mode {
    background-image: none;
    background: var(--page-bg);
  }
  body.zen-mode .page-viewport {
    padding: 48px 32px;
  }

  .zen-controls {
    position: fixed;
    bottom: 24px;
    left: 50%;
    transform: translateX(-50%);
    display: none;
    align-items: center;
    gap: 12px;
    padding: 10px 16px;
    background: rgba(26, 20, 16, 0.85);
    border-radius: 24px;
    box-shadow: 0 4px 16px rgba(0,0,0,0.2);
    backdrop-filter: blur(8px);
    z-index: 200;
    opacity: 0;
    transition: opacity 0.25s ease;
    pointer-events: none;
    user-select: none;
  }
  body.zen-mode .zen-controls {
    display: flex;
  }
  body.zen-mode.show-zen-controls .zen-controls {
    opacity: 1;
    pointer-events: auto;
  }
  .zen-btn {
    width: 36px; height: 36px;
    border-radius: 50%;
    background: rgba(255,255,255,0.08);
    border: none;
    color: rgba(255,255,255,0.85);
    font-size: 1rem;
    cursor: pointer;
    display: flex;
    align-items: center;
    justify-content: center;
    transition: background 0.15s;
  }
  .zen-btn:hover { background: rgba(255,255,255,0.18); }
  .zen-btn:disabled { opacity: 0.3; cursor: default; }
  .zen-page-indicator {
    font-family: 'DM Mono', monospace;
    font-size: 0.78rem;
    color: rgba(255,255,255,0.7);
    min-width: 60px;
    text-align: center;
  }
  .zen-exit {
    font-family: 'DM Mono', monospace;
    font-size: 0.65rem;
    letter-spacing: 0.08em;
    color: rgba(255,255,255,0.5);
    border-left: 1px solid rgba(255,255,255,0.15);
    padding-left: 12px;
    margin-left: 4px;
    cursor: pointer;
    text-transform: uppercase;
  }
  .zen-exit:hover { color: white; }
  .zen-divider {
    width: 1px;
    height: 22px;
    background: rgba(255,255,255,0.15);
    margin: 0 4px;
  }

  /* ── Book mode (two-page spread + flip animation) ──────────────────────── */

  .spread {
    display: flex;
    align-items: stretch;
    justify-content: center;
    gap: 0;
    perspective: 2400px;
    transform-origin: center center;
  }
  .spread .spread-page {
    position: relative;
    background: white;
    box-shadow: 0 4px 18px var(--shadow), 0 1px 4px rgba(0,0,0,0.08);
    overflow: hidden;
    flex: 0 0 auto;
    transition: box-shadow 0.3s;
  }
  .spread .spread-page img {
    display: block;
    width: 100%;
    height: 100%;
    object-fit: contain;
    background: white;
  }
  /* The gutter where the two pages meet — cover-on-right means the spread
     can have a single page (cover or back). When two pages, they sit edge-to-edge
     with a subtle inner shadow simulating the binding. */
  .spread .spread-page.left {
    box-shadow: -3px 4px 18px var(--shadow), inset -8px 0 12px -8px rgba(0,0,0,0.18);
    border-radius: 2px 0 0 2px;
  }
  .spread .spread-page.right {
    box-shadow: 3px 4px 18px var(--shadow), inset 8px 0 12px -8px rgba(0,0,0,0.18);
    border-radius: 0 2px 2px 0;
  }
  .spread .spread-page.solo {
    border-radius: 2px;
    box-shadow: 0 4px 18px var(--shadow), 0 1px 4px rgba(0,0,0,0.08);
  }

  /* The flipping page — positioned absolutely on top of the spread during a
     transition. We swap the visible faces via backface-visibility. */
  .flip-page {
    position: absolute;
    top: 0;
    bottom: 0;
    transform-style: preserve-3d;
    transition: transform 0.6s cubic-bezier(0.4, 0.05, 0.3, 1);
    transform-origin: left center;
    z-index: 5;
    pointer-events: none;
  }
  .flip-page.flipping-forward {
    /* When turning to the NEXT spread, the right page (currently visible)
       rotates from 0 to -180deg around its left edge */
    transform: rotateY(-180deg);
  }
  .flip-page.flip-back-origin {
    /* When turning to the PREVIOUS spread, the page rotates the other way */
    transform-origin: right center;
  }
  .flip-page.flipping-backward {
    transform: rotateY(180deg);
  }
  .flip-face {
    position: absolute;
    inset: 0;
    backface-visibility: hidden;
    -webkit-backface-visibility: hidden;
    background: white;
    overflow: hidden;
  }
  .flip-face img {
    display: block;
    width: 100%;
    height: 100%;
    object-fit: contain;
    background: white;
  }
  .flip-face.back {
    transform: rotateY(180deg);
  }

  /* Annotation status pill — appears in the reader toolbar for PDFs with
     a sidecar. Green when decoded successfully, amber when found but
     un-decodable. */
  .annotation-pill {
    display: inline-flex;
    align-items: center;
    gap: 4px;
    font-family: 'DM Mono', monospace;
    font-size: 0.66rem;
    padding: 3px 9px;
    border-radius: 12px;
    margin-left: 8px;
    user-select: none;
  }
  .annotation-pill.ok {
    background: #d4e8d4;
    color: #2d5a2d;
    border: 1px solid #a5c8a5;
  }
  .annotation-pill.warn {
    background: #fef0d4;
    color: #6e4f1c;
    border: 1px solid #d4ba7a;
  }
  .annotation-pill.warn:hover { background: #fce5b8; }
  .annotation-pill span:first-child { font-size: 0.85rem; }

  /* Reader-mode toggle buttons in the toolbar */
  .mode-toggle {
    display: flex;
    align-items: center;
    gap: 4px;
    border-left: 1px solid var(--paper-border);
    padding-left: 12px;
    margin-left: 4px;
  }
  .mode-toggle .btn-nav.active {
    background: var(--accent);
    color: white;
    border-color: var(--accent);
  }
  .mode-toggle .btn-nav.active:hover {
    background: var(--accent-light);
  }

  /* ── Screenshots grid + lightbox ───────────────────────────────────────── */
    display: grid;
    grid-template-columns: repeat(2, 1fr);
    gap: 8px;
    padding: 8px;
  }
  .screenshot-grid .ss-tile {
    position: relative;
    cursor: pointer;
    border-radius: 6px;
    overflow: hidden;
    background: var(--paper);
    border: 1px solid var(--paper-border);
    transition: all 0.15s;
    aspect-ratio: 0.75;  /* match Scribe page aspect */
  }
  .screenshot-grid .ss-tile:hover {
    transform: translateY(-2px);
    box-shadow: 0 4px 14px var(--shadow);
    border-color: var(--accent-light);
  }
  .screenshot-grid .ss-tile img {
    width: 100%;
    height: 100%;
    object-fit: cover;
    display: block;
  }
  .ss-tile .ss-meta {
    position: absolute;
    bottom: 0; left: 0; right: 0;
    background: linear-gradient(to top, rgba(0,0,0,0.7) 0%, transparent 100%);
    color: white;
    font-family: 'DM Mono', monospace;
    font-size: 0.62rem;
    padding: 16px 8px 6px 8px;
    line-height: 1.3;
  }

  /* Lightbox: takes over the reader pane when a screenshot is selected */
  .lightbox {
    position: relative;
    flex: 1;
    display: flex;
    flex-direction: column;
    overflow: hidden;
    background: var(--page-bg);
  }
  .lightbox-toolbar {
    background: var(--paper-dark);
    border-bottom: 1px solid var(--paper-border);
    padding: 10px 20px;
    display: flex;
    align-items: center;
    gap: 12px;
    flex-shrink: 0;
  }
  .lightbox-title {
    flex: 1;
    font-family: 'DM Mono', monospace;
    font-size: 0.78rem;
    color: var(--text);
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .lightbox-meta {
    font-size: 0.7rem;
    color: var(--text-muted);
    margin-left: 8px;
  }
  .lightbox-image-wrap {
    flex: 1;
    display: flex;
    align-items: center;
    justify-content: center;
    padding: 32px;
    overflow: auto;
  }
  .lightbox-image {
    max-width: 100%;
    max-height: 100%;
    object-fit: contain;
    box-shadow: 0 4px 16px var(--shadow);
    border-radius: 2px;
    background: white;
  }
  .btn-danger {
    background: #c44;
    border: none;
    color: white;
    padding: 7px 14px;
    border-radius: 6px;
    font-family: 'DM Mono', monospace;
    font-size: 0.72rem;
    cursor: pointer;
    transition: background 0.15s;
  }
  .btn-danger:hover { background: #d55; }
  .btn-danger:disabled { opacity: 0.5; cursor: not-allowed; }

  /* Confirmation overlay shown over the lightbox before deleting */
  .delete-confirm {
    position: absolute;
    inset: 0;
    background: rgba(0,0,0,0.6);
    display: flex;
    align-items: center;
    justify-content: center;
    z-index: 50;
  }
  .delete-confirm-card {
    background: white;
    padding: 24px;
    border-radius: 10px;
    max-width: 380px;
    box-shadow: 0 8px 32px rgba(0,0,0,0.3);
  }
  .delete-confirm-card h4 {
    font-family: 'Playfair Display', serif;
    font-size: 1.1rem;
    margin-bottom: 8px;
  }
  .delete-confirm-card p {
    font-size: 0.8rem;
    color: var(--text-muted);
    line-height: 1.5;
    margin-bottom: 16px;
  }
  .delete-confirm-actions {
    display: flex;
    justify-content: flex-end;
    gap: 8px;
  }
  .delete-progress {
    margin-top: 12px;
    font-family: 'DM Mono', monospace;
    font-size: 0.7rem;
    color: var(--text-muted);
    line-height: 1.5;
    white-space: pre-wrap;
    max-height: 120px;
    overflow-y: auto;
  }
</style>
</head>
<body>
<div class="shell">

  <header class="topbar">
    <div class="logo">
      <svg class="logo-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5">
        <path d="M2 3h6a4 4 0 0 1 4 4v14a3 3 0 0 0-3-3H2z"/>
        <path d="M22 3h-6a4 4 0 0 0-4 4v14a3 3 0 0 1 3-3h7z"/>
      </svg>
      Scribe Library
    </div>
    <button class="btn-sync" onclick="syncAll()">⇣ Sync Device</button>
    <button class="btn-upload" onclick="openUpload()">⇡ Upload</button>
    <button class="btn-icon" onclick="clearCache()">✕ Clear Cache</button>
    <div class="topbar-status">
      <span class="device-dot" id="device-dot"></span>
      <span id="status-text">Checking…</span>
    </div>
  </header>

  <div id="warn-banner" class="warn-banner" style="display:none"></div>

  <div id="sync-drawer" style="display:none">
    <span class="sync-close" onclick="hideSyncDrawer()" title="Close">✕</span>
    <div id="sync-log"></div>
    <div class="sync-progress-track" id="sync-progress-track" style="display:none">
      <div class="sync-progress-fill" id="sync-progress-fill"></div>
    </div>
    <div id="sync-progress-msg"></div>
  </div>

  <nav class="sidebar">
    <div class="tabs">
      <div class="tab active" data-tab="documents" onclick="switchTab('documents')">
        Documents <span class="tab-count" id="count-documents">0</span>
      </div>
      <div class="tab" data-tab="notebooks" onclick="switchTab('notebooks')">
        Notebooks <span class="tab-count" id="count-notebooks">0</span>
      </div>
      <div class="tab" data-tab="screenshots" onclick="switchTab('screenshots')">
        Screenshots <span class="tab-count" id="count-screenshots">0</span>
      </div>
      <div class="tab" data-tab="cloud" onclick="switchTab('cloud')" title="Your Amazon account - works without the cable">
        ☁ Cloud <span class="tab-count" id="count-cloud">0</span>
      </div>
    </div>

    <div class="sidebar-actions">
      <input class="search-box" type="text" placeholder="Filter…"
        oninput="setFilter(this.value)" id="search-input">
      <div class="filter-chips" id="filter-chips"></div>
    </div>

    <div class="selection-bar" id="selection-bar" style="display:none">
      <span id="sel-count"></span>
      <button class="primary" id="sel-download" onclick="downloadSelected()">⤓ Download</button>
      <button onclick="clearSelection()">Clear</button>
    </div>

    <div class="item-list" id="item-list">
      <div class="empty-state">Loading…</div>
    </div>
  </nav>

  <main class="reader" id="reader">
    <div class="welcome">
      <svg class="welcome-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1">
        <path d="M2 3h6a4 4 0 0 1 4 4v14a3 3 0 0 0-3-3H2z"/>
        <path d="M22 3h-6a4 4 0 0 0-4 4v14a3 3 0 0 1 3-3h7z"/>
      </svg>
      <h2>Choose something to read</h2>
      <p>Pick a document or notebook from the list. Documents are the PDFs you sent to the Scribe — opened with your handwritten annotations overlaid.</p>
      <p style="font-family:'DM Mono',monospace;font-size:0.7rem;opacity:0.6">
        Once reading: <strong>z</strong> for zen mode · <strong>b</strong> for book mode · <strong>← →</strong> to flip pages
      </p>
    </div>
  </main>

</div>

<!-- Upload dialog -->
<div class="modal-bg" id="upload-modal" onclick="if (event.target === this) closeUpload()">
  <div class="modal" role="dialog" aria-labelledby="upload-title">
    <h3 id="upload-title">Upload to Kindle</h3>
    <p>Add documents from this computer to your Kindle.</p>
    <div class="drop-zone" id="drop-zone" onclick="document.getElementById('upload-input').click()">
      <div class="drop-zone-text" id="drop-zone-text">Drop files here or click to choose</div>
      <div class="drop-zone-hint">PDF, EPUB, Word, TXT, RTF, HTML, images</div>
    </div>
    <input type="file" id="upload-input" multiple style="display:none"
      onchange="setUploadFiles(this.files)">
    <div class="upload-methods">
      <label class="upload-method">
        <input type="radio" name="upload-method" value="stk" checked onchange="refreshUploadButton()">
        <span><strong>Send to Kindle</strong> (recommended)
          <small id="stk-hint">Opens Amazon's Send to Kindle app with these files. Delivered over Wi-Fi to your Kindle, and you can write on the PDFs.</small></span>
      </label>
      <label class="upload-method">
        <input type="radio" name="upload-method" value="usb" onchange="refreshUploadButton()">
        <span><strong>Copy over USB</strong>
          <small id="usb-hint">Instant and offline. Copied to documents/Uploads. PDF, EPUB and TXT only; handwriting on sideloaded files may be limited.</small></span>
      </label>
    </div>
    <div class="upload-status" id="upload-status"></div>
    <div class="modal-actions">
      <button class="btn-modal" onclick="closeUpload()">Close</button>
      <button class="btn-modal primary" id="upload-go" onclick="doUpload()" disabled>Upload</button>
    </div>
  </div>
</div>

<!-- Zen mode floating controls (visible only in zen mode, fade with mouse activity) -->
<div class="zen-controls" id="zen-controls">
  <button class="zen-btn" id="zen-prev" onclick="goPage(-1)" title="Previous page (←)">‹</button>
  <span class="zen-page-indicator" id="zen-indicator">1 / 1</span>
  <button class="zen-btn" id="zen-next" onclick="goPage(1)" title="Next page (→)">›</button>
  <span class="zen-divider"></span>
  <button class="zen-btn" onclick="changeZoom(-0.15)" title="Zoom out (−)">−</button>
  <span class="zen-page-indicator" id="zen-zoom-label" style="min-width:48px">100%</span>
  <button class="zen-btn" onclick="changeZoom(0.15)" title="Zoom in (+)">+</button>
  <button class="zen-btn" onclick="resetZoom()" title="Reset zoom (0)">⊡</button>
  <span class="zen-exit" onclick="toggleZen()" title="Exit zen mode (Esc)">Exit</span>
</div>

<script>
// ── State ────────────────────────────────────────────────────────────────────
let library = { documents: [], notebooks: [], screenshots: [] };
let cloud = { status: null, documents: [], notebooks: [], error: null, loading: false };
let _cloudPoll = null;
let currentTab = 'documents';
let currentItem = null;
let currentPage = 0;
let totalPages = 0;
let zoom = 1.0;
let filterText = '';
let activeDocFilter = 'all';
let selected = new Set();
let _syncTimer = null;
// Reader modes — bookMode persists across sessions, zen is per-session
let bookMode = false;
try {
  bookMode = window.localStorage && window.localStorage.getItem('bookMode') === '1';
} catch(e){}

// ── Init ─────────────────────────────────────────────────────────────────────
async function init() {
  await loadStatus();
  await loadLibrary();
  setInterval(loadStatus, 5000);
  document.addEventListener('keydown', handleKey);

  // Deep links: /?tab=notebooks&filter=Planner&open=<itemId>
  const params = new URLSearchParams(window.location.search);
  if (params.get('tab')) switchTab(params.get('tab'));
  if (params.get('filter')) {
    filterText = params.get('filter');
    document.getElementById('search-input').value = filterText;
    renderList();
  }
  if (params.get('open')) openItem(params.get('open'));

  // When the window resizes, recompute page dimensions so things still fit
  // the viewport. We touch only inline styles, not innerHTML, so images
  // don't reload. Throttled to avoid layout thrash.
  let resizeTimer = null;
  window.addEventListener('resize', () => {
    if (resizeTimer) clearTimeout(resizeTimer);
    resizeTimer = setTimeout(() => {
      if (currentItem) resizePageContainers();
    }, 150);
  });
}

async function loadStatus() {
  try {
    const r = await fetch('/api/status');
    const s = await r.json();
    const dot = document.getElementById('device-dot');
    const txt = document.getElementById('status-text');
    const pl = (n, w) => `${n} ${w}${n === 1 ? '' : 's'}`;
    const counts = `${pl(s.document_count, 'document')} · ${pl(s.notebook_count, 'notebook')} · ${pl(s.screenshot_count, 'screenshot')}`;
    if (s.device_connected) {
      dot.classList.add('connected');
      txt.textContent = `${s.device_name} · ${counts}`;
    } else {
      dot.classList.remove('connected');
      txt.textContent = (s.document_count + s.notebook_count + s.screenshot_count > 0)
        ? `${counts} (disconnected)`
        : 'No Kindle connected';
    }
    const banner = document.getElementById('warn-banner');
    if (!s.converter_ready && s.converter_message) {
      banner.textContent = '⚠ ' + s.converter_message.split('\n')[0];
      banner.style.display = 'block';
    } else { banner.style.display = 'none'; }
  } catch(e) {
    document.getElementById('status-text').textContent = 'Server error';
  }
}

async function loadLibrary() {
  try {
    const r = await fetch('/api/library');
    library = await r.json();
    if (!library.screenshots) library.screenshots = [];
    document.getElementById('count-documents').textContent = library.documents.length;
    document.getElementById('count-notebooks').textContent = library.notebooks.length;
    document.getElementById('count-screenshots').textContent = library.screenshots.length;
    renderList();
    loadCloud(false);
  } catch(e) {
    document.getElementById('item-list').innerHTML =
      `<div class="empty-state">Failed to load library: ${escHtml(e.message)}</div>`;
  }
}

// ── Tabs & filtering ─────────────────────────────────────────────────────────
// ── Cloud (Amazon account) ───────────────────────────────────────────────────
async function loadCloud(refresh) {
  try {
    cloud.status = await (await fetch('/api/cloud/status')).json();
  } catch (e) { return; }
  if (cloud.status.signed_in && !cloud.status.login_running) {
    cloud.loading = true;
    if (currentTab === 'cloud') renderList();
    try {
      const r = await fetch('/api/cloud/library' + (refresh ? '?refresh=1' : ''));
      const d = await r.json();
      if (d.ok) {
        cloud.documents = d.documents || [];
        cloud.notebooks = d.notebooks || [];
        cloud.error = d.notebook_error ? ('Notebooks: ' + d.notebook_error) : null;
        cloud.fetched_at = d.fetched_at;
      } else {
        cloud.error = d.error;
        if (d.auth) cloud.status.signed_in = false;
      }
    } catch (e) { cloud.error = e.message; }
    cloud.loading = false;
  }
  document.getElementById('count-cloud').textContent =
    cloud.documents.length + cloud.notebooks.length;
  if (currentTab === 'cloud') renderList();
}

async function cloudSignIn() {
  const store = document.getElementById('cloud-store').value;
  localStorage.setItem('cloudStore', store);
  await fetch('/api/cloud/login', { method: 'POST',
    headers: {'Content-Type': 'application/json'}, body: JSON.stringify({store}) });
  cloud.status = Object.assign(cloud.status || {}, {login_running: true, login_error: null});
  renderList();
  if (_cloudPoll) clearInterval(_cloudPoll);
  _cloudPoll = setInterval(async () => {
    const st = await (await fetch('/api/cloud/status')).json();
    if (!st.login_running) {
      clearInterval(_cloudPoll); _cloudPoll = null;
      cloud.status = st;
      if (st.signed_in) await loadCloud(true); else renderList();
    }
  }, 1500);
}

async function cloudSignOut() {
  if (!confirm('Sign out of Amazon? The saved session is deleted from this computer.')) return;
  await fetch('/api/cloud/logout', { method: 'POST' });
  cloud = { status: { signed_in: false, stores: cloud.status?.stores }, documents: [], notebooks: [] };
  document.getElementById('count-cloud').textContent = 0;
  renderList();
}

function renderCloudPanel() {
  const st = cloud.status || {};
  if (st.login_running) {
    return `<div class="cloud-panel"><h4>Signing in…</h4>
      Finish signing in in the Amazon window. Amazon may ask a second time for
      Kindle for Web (notebooks); that's expected. The window closes by itself.</div>`;
  }
  const stores = st.stores || ['amazon.com'];
  const saved = localStorage.getItem('cloudStore') || 'amazon.com';
  return `<div class="cloud-panel">
    <h4>Your Kindle, without the cable</h4>
    Like the Kindle phone apps, this reads your Amazon account: documents you
    sent to your Kindle and your Scribe notebooks, synced over Wi-Fi.
    You sign in on Amazon's own page; only the session is kept on this computer,
    encrypted for your Windows account.
    <select id="cloud-store">${stores.map(s =>
      `<option ${s === saved ? 'selected' : ''}>${s}</option>`).join('')}</select>
    <button class="btn-modal primary" onclick="cloudSignIn()">Sign in to Amazon</button>
    ${st.login_error ? `<p class="err" style="color:#b33;margin-top:8px">${escHtml(st.login_error)}</p>` : ''}
    <p style="margin-top:10px;font-size:0.7rem;opacity:0.75">Uses Amazon's unofficial web
    services (the ones its own sites use); they can change without notice. Annotated
    PDFs with your ink still come over USB (Documents tab).</p>
  </div>`;
}

function renderCloudList(items) {
  const st = cloud.status || {};
  const bar = `<div class="cloud-bar"><span>☁ ${escHtml(st.store || '')}${cloud.loading ? ' · loading…' : ''}</span>
    <button onclick="loadCloud(true)" title="Fetch the latest list from Amazon">↻ Refresh</button>
    <button onclick="cloudSignOut()">Sign out</button></div>`;
  const err = cloud.error ? `<div class="empty-state" style="padding:8px 12px;color:#b33">${escHtml(cloud.error)}</div>` : '';
  const docs = items.filter(i => i.kind === 'cloud_document');
  const nbs = items.filter(i => i.kind === 'cloud_notebook');
  let html = bar + err;
  if (nbs.length) html += `<div class="cloud-section">Notebooks (${nbs.length})</div>` + nbs.map(renderCard).join('');
  if (docs.length) html += `<div class="cloud-section">Documents (${docs.length})</div>` + docs.map(renderCard).join('');
  if (!nbs.length && !docs.length && !cloud.loading)
    html += `<div class="empty-state">Nothing found in your Amazon library${filterText ? ' for "' + escHtml(filterText) + '"' : ''}.</div>`;
  return html;
}

function switchTab(tab) {
  currentTab = tab;
  document.querySelectorAll('.tab').forEach(el => {
    el.classList.toggle('active', el.dataset.tab === tab);
  });
  renderFilterChips();
  renderList();
}

function renderFilterChips() {
  const wrap = document.getElementById('filter-chips');
  const selectAll = `<span class="chip" onclick="selectAllVisible()" title="Select everything currently listed">Select all</span>`;
  if (currentTab === 'documents') {
    wrap.innerHTML = `
      <span class="chip ${activeDocFilter==='all'?'active':''}" onclick="setDocFilter('all')">All</span>
      <span class="chip ${activeDocFilter==='annotated'?'active':''}" onclick="setDocFilter('annotated')">Annotated</span>
      ${selectAll}
    `;
  } else {
    wrap.innerHTML = selectAll;
  }
}

function selectAllVisible() {
  for (const it of currentItems()) selected.add(it.id);
  renderSelectionBar();
  renderList();
}

function setDocFilter(f) { activeDocFilter = f; renderFilterChips(); renderList(); }
function setFilter(v) { filterText = v; renderList(); }

function currentItems() {
  let items;
  if (currentTab === 'documents') items = library.documents;
  else if (currentTab === 'notebooks') items = library.notebooks;
  else if (currentTab === 'cloud') items = [...cloud.notebooks, ...cloud.documents];
  else items = library.screenshots || [];

  const t = filterText.toLowerCase();
  return items
    .filter(it => !t ||
      (it.name||'').toLowerCase().includes(t) ||
      (it.filename||'').toLowerCase().includes(t))
    .filter(it => currentTab !== 'documents' || activeDocFilter === 'all' ||
                   (it.annotated_pages || 0) > 0);
}

// ── List rendering ───────────────────────────────────────────────────────────
function renderList() {
  const list = document.getElementById('item-list');
  const items = currentItems();

  if (currentTab === 'cloud') {
    list.innerHTML = (cloud.status && cloud.status.signed_in && !cloud.status.login_running)
      ? renderCloudList(items) : renderCloudPanel();
    return;
  }

  if (!items.length) {
    let all;
    if (currentTab === 'documents') all = library.documents;
    else if (currentTab === 'notebooks') all = library.notebooks;
    else all = library.screenshots || [];
    list.innerHTML = all.length === 0
      ? `<div class="empty-state">No ${currentTab} found.<br><br>
          Click <strong>Sync Device</strong> above to pull from your Kindle Scribe.</div>`
      : `<div class="empty-state">No matches for "${escHtml(filterText)}"</div>`;
    return;
  }

  list.innerHTML = items.map(it => renderCard(it)).join('');
}

function formatDateTime(iso) {
  if (!iso) return '';
  const d = new Date(iso);
  // E.g. "Apr 29, 2025, 10:30 AM"
  return d.toLocaleString('en-US', {
    year:'numeric', month:'short', day:'numeric',
    hour:'numeric', minute:'2-digit',
  });
}

function renderCard(it) {
  const isActive = currentItem?.id === it.id;
  const thumbHtml = renderThumb(it);
  const badge = renderBadge(it);
  const annMeta = (it.kind === 'document' && it.annotated_pages)
    ? `<span title="annotated pages">${it.annotated_pages} ✏</span>` : '';
  const isCloud = it.kind.startsWith('cloud_');
  const size = it.kind === 'screenshot' ? `${it.size_kb}kb`
    : (isCloud ? (it.folder ? escHtml(it.folder) : '') : `${it.size_mb}mb`);
  return `
    <div class="nb-card ${isActive?'active':''} ${it.unsupported?'unsupported-card':''}" data-kind="${it.kind}"
         onclick="openItem('${it.id}')">
      ${isCloud ? '' : `<input type="checkbox" class="sel-box" ${selected.has(it.id)?'checked':''}
             onclick="toggleSelect(event, '${it.id}')" title="Select for download">`}
      ${it.kind !== 'screenshot' && !isCloud
        ? `<span class="card-rename" title="Rename" onclick="renameItem('${it.id}', event)">✎</span>`
        : ''}
      ${thumbHtml}
      <div class="nb-info">
        ${badge}
        <div class="nb-name">${formatName(it)}</div>
        <div class="nb-meta">
          ${annMeta}
          <span>${size}</span>
          <span title="${it.kind === 'document' || it.kind === 'cloud_document'
            ? (it.added ? 'Added' : 'Last modified')
            : (it.kind.endsWith('notebook') ? 'Last written on' : 'Captured')}">${formatDate(it.added || it.modified)}</span>
        </div>
      </div>
    </div>`;
}

function renderThumb(it) {
  if (it.kind === 'screenshot') {
    return `<img class="nb-thumb" src="/screenshot/${it.id}" alt="" loading="lazy">`;
  }
  if (it.kind === 'notebook' && it.has_thumbnail) {
    return `<img class="nb-thumb" src="/thumbnail/${it.id}" alt="">`;
  }
  if (it.kind.startsWith('cloud_')) {
    return it.has_cover
      ? `<img class="nb-thumb" src="/cover/${it.id}" alt="" onerror="this.style.display='none'">`
      : `<div class="nb-thumb-placeholder ${it.kind === 'cloud_notebook' ? 'notebook' : 'book'}">☁</div>`;
  }
  if (it.kind === 'document' && it.has_cover) {
    return `<img class="nb-thumb" src="/cover/${it.id}" alt="" onerror="this.style.display='none'">`;
  }
  if (it.kind === 'notebook') {
    return `<div class="nb-thumb-placeholder notebook">.nbk</div>`;
  }
  // Document not yet opened — show a colored tile with its original format
  return `<div class="nb-thumb-placeholder book">${escHtml(it.format || 'PDF')}</div>`;
}

function renderBadge(it) {
  if (it.kind === 'document') {
    const fmt = `<span class="nb-badge fmt">${escHtml(it.format || 'PDF')}</span>`;
    if (it.unsupported)
      return fmt + ' <span class="nb-badge unsupported">Unsupported</span>';
    const ann = it.annotated_pages
      ? ` <span class="nb-badge annotated">✏ Annotated</span>` : '';
    const upd = (it.exported && it.stale)
      ? ` <span class="nb-badge updated" title="Changed since the last export — reopen to refresh">New ink</span>` : '';
    return fmt + ann + upd;
  }
  if (it.kind === 'notebook')
    return '<span class="nb-badge scribe">Scribe</span>';
  if (it.kind === 'cloud_notebook')
    return '<span class="nb-badge cloud">☁ Notebook</span>';
  if (it.kind === 'cloud_document')
    return '<span class="nb-badge cloud">☁ Document</span>';
  return '';
}

function formatName(it) {
  // Screenshots are titled by their capture time
  if (it.kind === 'screenshot') {
    return escHtml(formatDateTime(it.captured_at || it.modified));
  }
  const name = it.title || it.name;
  // For notebooks with UUID names, show abbreviated
  if (it.kind === 'notebook' && /^[a-f0-9]{8}-/.test(name)) {
    return `Notebook <span style="opacity:0.4;font-size:0.85em">#${name.slice(0,8)}</span>`;
  }
  return escHtml(name);
}

// ── Selection & bulk download ────────────────────────────────────────────────
function toggleSelect(e, id) {
  e.stopPropagation();
  if (e.target.checked) selected.add(id); else selected.delete(id);
  renderSelectionBar();
}

function clearSelection() {
  selected.clear();
  renderSelectionBar();
  renderList();
}

function renderSelectionBar() {
  const bar = document.getElementById('selection-bar');
  bar.style.display = selected.size ? 'flex' : 'none';
  document.getElementById('sel-count').textContent = `${selected.size} selected`;
}

async function downloadSelected() {
  if (!selected.size) return;
  const btn = document.getElementById('sel-download');
  const orig = btn.textContent;
  btn.disabled = true;
  btn.textContent = 'Starting…';
  try {
    const r = await fetch('/api/download_zip', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ids: [...selected]}),
    });
    const d = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(d.error || `HTTP ${r.status}`);

    const downloadId = await pollZipProgress(btn);
    const rr = await fetch(`/api/zip_result/${downloadId}`);
    if (!rr.ok) throw new Error(`HTTP ${rr.status}`);
    const blob = await rr.blob();
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = 'scribe_export.zip';
    a.click();
    URL.revokeObjectURL(a.href);
  } catch (e) {
    alert('Download failed: ' + e.message);
  } finally {
    btn.disabled = false;
    btn.textContent = orig;
  }
}

function pollZipProgress(btn) {
  return new Promise((resolve, reject) => {
    const timer = setInterval(async () => {
      let s;
      try {
        s = await (await fetch('/api/zip_progress')).json();
      } catch(e) { return; }

      if (s.total > 0) {
        btn.textContent = `${s.current}/${s.total}… ${(s.name||'').slice(0,24)}`;
      }
      if (!s.running) {
        clearInterval(timer);
        if (s.error) reject(new Error(s.error));
        else if (s.download_id) resolve(s.download_id);
        else reject(new Error('Export finished with no result'));
      }
    }, 500);
  });
}

function formatDate(iso) {
  if (!iso) return '';
  const d = new Date(iso);
  return d.toLocaleDateString('en-US', {year:'numeric', month:'short', day:'numeric'});
}

function escHtml(s) {
  return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}

// ── Open + read ──────────────────────────────────────────────────────────────
async function openItem(id) {
  // Find across all three lists
  const allItems = [
    ...library.documents,
    ...library.notebooks,
    ...(library.screenshots || []),
    ...cloud.notebooks,
    ...cloud.documents,
  ];
  const it = allItems.find(i => i.id === id);
  if (!it) return;
  currentItem = it;
  currentPage = 0;
  // Don't carry zen mode across item switches — that would feel like a trap
  if (document.body.classList.contains('zen-mode')) {
    document.body.classList.remove('zen-mode');
    unbindZenHover();
  }
  renderList();

  // Screenshots get a dedicated lightbox view, not the paged reader
  if (it.kind === 'screenshot') {
    showScreenshot(it);
    return;
  }

  const reader = document.getElementById('reader');
  reader.innerHTML = `
    <div class="reader-toolbar">
      <div class="reader-title">${formatName(it)}</div>
    </div>
    <div class="page-viewport" style="position:relative">
      <div class="loading-overlay">
        <div class="spinner"></div>
        <div class="loading-text">${it.kind.startsWith('cloud_') ? 'Fetching from Amazon…'
          : it.kind === 'notebook' ? 'Decoding handwriting…'
          : (it.annotated_pages ? 'Preparing annotated PDF…' : 'Extracting document…')}</div>
        <div class="loading-sub">${it.kind === 'notebook'
          ? 'First open of a notebook takes a few seconds — strokes are being rendered. Cached for next time.'
          : (it.annotated_pages
             ? 'First open extracts the document and overlays your annotations. Cached for next time.'
             : 'First open recovers the original document from the device copy. Cached for next time.')}</div>
      </div>
    </div>
  `;

  const endpoint = it.kind.startsWith('cloud_') ? `/api/cloud/item/${id}`
    : it.kind === 'notebook' ? `/api/notebook/${id}` : `/api/document/${id}`;

  // Live conversion progress in the loading overlay
  const progTimer = setInterval(async () => {
    if (currentItem?.id !== id) return;
    try {
      const s = await (await fetch(`/api/convert_progress/${id}`)).json();
      const el = document.querySelector('.loading-text');
      if (s.message && el) el.textContent = s.message;
    } catch(e) {}
  }, 700);

  try {
    const r = await fetch(endpoint);
    // The user may have opened something else while this one was converting
    // (conversion continues server-side and stays cached) — don't let a
    // slow response steal the reader from the current item.
    if (currentItem?.id !== id) return;
    if (!r.ok) {
      const err = await r.json();
      throw new Error(err.error || `HTTP ${r.status}`);
    }
    const data = await r.json();

    if (data.fallback_pdf) {
      // Page extraction didn't work — show the PDF in an iframe
      showPdfFallback(it, data);
      return;
    }

    if (!data.pages || data.pages.length === 0) {
      showEmptyMessage(it);
      return;
    }

    buildPagedReader(it, data);
  } catch(e) {
    if (currentItem?.id !== id) return;
    showError(it, e.message);
    // A failed attempt may have produced new knowledge (e.g. an
    // "unsupported" verdict) — refresh the list badges right away.
    loadLibrary();
  } finally {
    clearInterval(progTimer);
  }
}

function buildPagedReader(it, data) {
  totalPages = data.pages.length;
  currentPage = 0;
  window._pages = data.pages;

  // Book mode is only allowed for items with at least 3 pages and not for
  // raw PDFs (data.fallback_pdf would have been true for those anyway).
  const bookModeAllowed = totalPages >= 3;
  // Persisted preference — but we won't enable it on items where it's not allowed
  if (!bookModeAllowed) bookMode = false;

  const reader = document.getElementById('reader');
  reader.innerHTML = `
    <div class="reader-toolbar">
      <div class="reader-title">
        ${it.kind.startsWith('cloud_') ? `<span>${formatName(it)}</span>`
          : `<span style="cursor:pointer" title="Click to rename"
              onclick="renameItem('${it.id}')">${formatName(it)}</span>`}
        <em style="margin-left:8px">· ${totalPages} page${totalPages!==1?'s':''}</em>
        ${it.kind === 'document' && it.annotated_pages
          ? `<span class="annotation-pill ok" title="Pages with handwriting baked into this PDF"><span>✏</span> ${it.annotated_pages} annotated page${it.annotated_pages!==1?'s':''}</span>`
          : ''}
      </div>
      <div class="page-controls">
        <button class="btn-nav" id="btn-prev" onclick="goPage(-1)" title="Previous (←)">‹</button>
        <span class="page-indicator" id="page-indicator">1 / ${totalPages}</span>
        <button class="btn-nav" id="btn-next" onclick="goPage(1)" title="Next (→)">›</button>
      </div>
      <div class="zoom-controls">
        <button class="btn-nav" onclick="changeZoom(-0.15)" title="Zoom out (−)">−</button>
        <span class="zoom-label" id="zoom-label">100%</span>
        <button class="btn-nav" onclick="changeZoom(0.15)" title="Zoom in (+)">+</button>
        <button class="btn-nav" onclick="resetZoom()" title="Fit to screen (0)">⊡</button>
      </div>
      <div class="mode-toggle">
        <button class="btn-nav ${bookMode?'active':''}" onclick="toggleBookMode()"
                ${bookModeAllowed?'':'disabled'}
                title="${bookModeAllowed?'Book mode (b)':'Need 3+ pages for book mode'}">⛶</button>
        <button class="btn-nav" onclick="toggleZen()" title="Zen mode (z)">☯</button>
      </div>
      <a class="btn-icon" style="color:var(--text);border-color:var(--paper-border)"
         href="${data.pdf_url}" target="_blank" rel="noopener" title="Download PDF">⤓ PDF</a>
    </div>
    <div class="page-viewport" id="page-viewport">
      <div class="page-wrapper" id="page-wrapper">
        <!-- Content rendered by renderPage() based on bookMode -->
      </div>
    </div>
    <div class="thumb-strip" id="thumb-strip">
      ${data.pages.map((p, i) => `
        <img class="thumb ${i===0?'active':''}" src="${p}" loading="lazy"
             onclick="jumpPage(${i})" id="thumb-${i}">
      `).join('')}
    </div>
  `;
  // Apply book mode class on body so global CSS hides the thumb strip
  document.body.classList.toggle('book-mode', bookMode);

  // Mouse wheel flips pages — unless the page overflows the viewport
  // (zoomed in), in which case the wheel keeps its scrolling job.
  const vp = document.getElementById('page-viewport');
  let lastWheel = 0;
  vp.addEventListener('wheel', (e) => {
    if (vp.scrollHeight > vp.clientHeight + 2 || vp.scrollWidth > vp.clientWidth + 2) return;
    e.preventDefault();
    const now = Date.now();
    if (now - lastWheel < 200) return;
    lastWheel = now;
    goPage(e.deltaY > 0 ? 1 : -1);
  }, { passive: false });

  showPage(0);
}

function showPage(n) {
  if (n < 0 || n >= totalPages) return;

  // In book mode, snap n to even-when-paired layout (cover-on-right model):
  //  spread 0: page 0 alone (cover, on right)
  //  spread 1: pages [1, 2]
  //  spread 2: pages [3, 4]
  //  spread k (k>=1): pages [2k-1, 2k]
  if (bookMode) {
    n = snapToSpreadStart(n);
  }

  const oldPage = currentPage;
  currentPage = n;
  updateChrome();

  // Decide whether to animate (book mode + adjacent spread) or just swap
  const shouldAnimate = bookMode && shouldFlipAnimate(oldPage, n);
  if (shouldAnimate) {
    animateFlip(oldPage, n);
  } else {
    renderPage();
  }
}

function isCoverOnRight() {
  // Documents follow the codex convention: page 0 is a cover/title page that
  // sits alone on the right of the first spread.
  // Notebooks don't have a "cover" — they're filled from page 1 — so they
  // pair from the start: [0,1], [2,3], [4,5]…
  return currentItem && currentItem.kind === 'document';
}

function snapToSpreadStart(n) {
  // Return the leftmost page index of the spread containing page n.
  if (isCoverOnRight()) {
    // Books: spread 0 = [_, 0], spread k>=1 = [2k-1, 2k]
    if (n <= 0) return 0;
    return n % 2 === 1 ? n : n - 1;
  }
  // Notebooks: spread k = [2k, 2k+1]
  return n - (n % 2);
}

function spreadPagesFor(n) {
  // Return [leftPageIndex|null, rightPageIndex|null] for the spread starting at n.
  if (isCoverOnRight()) {
    if (n === 0) return [null, 0];
    return [n, (n + 1 < totalPages) ? n + 1 : null];
  }
  // Notebooks: simple paired layout, no solo cover
  return [n, (n + 1 < totalPages) ? n + 1 : null];
}

function spreadIndexOf(n) {
  // Map a page index to a 0-based spread number.
  if (isCoverOnRight()) {
    return n === 0 ? 0 : Math.floor((n + 1) / 2);
  }
  return Math.floor(n / 2);
}

function shouldFlipAnimate(oldN, newN) {
  if (oldN === newN) return false;
  // Animation looks wrong when the wrapper is scaled (zoom != 1) because the
  // flip-page is positioned in untransformed coords. Skip animation in that
  // case — the spread still renders, just without the flip transition.
  if (Math.abs(zoom - 1.0) > 0.01) return false;
  // Animate only when moving to an adjacent spread; jumping farther just snaps.
  if (Math.abs(spreadIndexOf(oldN) - spreadIndexOf(newN)) !== 1) return false;
  // Skip animation when either the leaving or arriving spread is solo
  // (cover-on-right or trailing single page) — the gutter math doesn't apply.
  const oldSolo = spreadPagesFor(snapToSpreadStart(oldN)).filter(x => x !== null).length === 1;
  const newSolo = spreadPagesFor(snapToSpreadStart(newN)).filter(x => x !== null).length === 1;
  if (oldSolo || newSolo) return false;
  return true;
}

function renderPage() {
  const wrapper = document.getElementById('page-wrapper');
  if (!wrapper) return;

  if (bookMode) {
    const [left, right] = spreadPagesFor(currentPage);
    wrapper.innerHTML = renderSpreadHtml(left, right);
  } else {
    // Single-page mode uses the same fit-to-viewport sizing as book mode so
    // 100% means "page fills the available space without scrolling" — not
    // "natural pixel size of the rasterized image" (which is way bigger).
    const url = window._pages[currentPage];
    wrapper.innerHTML = `
      <div class="single-page" id="single-page">
        <img class="page-img page-shadow" id="page-img"
             src="${url}" alt="Page" draggable="false">
      </div>
    `;
  }
  resizePageContainers();
  applyZoom();
}

function resizePageContainers() {
  // Recompute and apply the per-page pixel dimensions. Called on initial
  // render AND on window resize — touches only the .style of existing
  // elements, never the innerHTML, so images don't reload.
  if (bookMode) {
    const size = pageSize(/*paired=*/true);
    document.querySelectorAll('.spread-page').forEach(el => {
      el.style.cssText = size;
    });
  } else {
    const single = document.getElementById('single-page');
    if (single) single.style.cssText = pageSize(/*paired=*/false);
  }
}

function renderSpreadHtml(leftIdx, rightIdx) {
  // Two pages side by side, sized to fit the viewport.
  // .solo is for single-page spreads (cover on its own, or a final lone page).
  const isSolo = (leftIdx === null) !== (rightIdx === null);
  const size = pageSize(/*paired=*/true);
  if (isSolo) {
    const idx = leftIdx ?? rightIdx;
    const url = window._pages[idx];
    return `
      <div class="spread">
        <div class="spread-page solo" id="spread-solo" style="${size}">
          <img src="${url}" alt="Page ${idx+1}">
        </div>
      </div>`;
  }
  return `
    <div class="spread">
      <div class="spread-page left" id="spread-left" style="${size}">
        <img src="${window._pages[leftIdx]}" alt="Page ${leftIdx+1}">
      </div>
      <div class="spread-page right" id="spread-right" style="${size}">
        <img src="${window._pages[rightIdx]}" alt="Page ${rightIdx+1}">
      </div>
    </div>`;
}

function pageSize(paired) {
  // Compute pixel dimensions for ONE page that:
  //   - preserves the Scribe aspect ratio (1860:2480 ≈ 0.75)
  //   - fits inside the page-viewport with comfortable padding
  //   - allows the requested layout: paired=true means two pages side-by-side
  //     (so one page gets half the viewport width); paired=false means a
  //     single page can use the full viewport width.
  const viewport = document.getElementById('page-viewport');
  if (!viewport) return 'width:400px;height:560px';
  const padding = 64;  // total horizontal/vertical padding allowance
  const availH = viewport.clientHeight - padding;
  const availW = paired
    ? (viewport.clientWidth - padding) / 2
    : (viewport.clientWidth - padding);
  const aspect = 1860 / 2480;  // width:height
  // Start from the height limit and clamp by width
  let h = availH;
  let w = h * aspect;
  if (w > availW) {
    w = availW;
    h = w / aspect;
  }
  return `width:${w}px;height:${h}px`;
}

// Keep the old name as an alias so animateFlip's call site stays working.
function spreadPageSize() {
  return pageSize(/*paired=*/true);
}

function animateFlip(oldN, newN) {
  // Build a flip-page overlay: a page-sized element rotating around the
  // gutter, with TWO faces — front face shows the page leaving, back face
  // shows the page coming in. CSS `backface-visibility: hidden` swaps which
  // is visible halfway through the rotation.
  const forward = newN > oldN;
  const wrapper = document.getElementById('page-wrapper');
  if (!wrapper) { renderPage(); return; }

  // Determine which page leaves and which arrives
  // Forward: from old spread's right page → next spread's left page
  // Backward: from old spread's left page → previous spread's right page
  let leavingIdx, arrivingIdx;
  if (forward) {
    const oldSpread = spreadPagesFor(oldN);
    leavingIdx = oldSpread[1] !== null ? oldSpread[1] : oldSpread[0];
    const newSpread = spreadPagesFor(newN);
    arrivingIdx = newSpread[0] !== null ? newSpread[0] : newSpread[1];
  } else {
    const oldSpread = spreadPagesFor(oldN);
    leavingIdx = oldSpread[0] !== null ? oldSpread[0] : oldSpread[1];
    const newSpread = spreadPagesFor(newN);
    arrivingIdx = newSpread[1] !== null ? newSpread[1] : newSpread[0];
  }

  if (leavingIdx === null || arrivingIdx === null) {
    renderPage();
    return;
  }

  // First, render the destination spread underneath
  renderPage();
  // The flip-page overlay sits on top and animates
  const flipPage = document.createElement('div');
  flipPage.className = 'flip-page' + (forward ? '' : ' flip-back-origin');
  flipPage.style.cssText = spreadPageSize();
  // Position: forward = on the right side of gutter; backward = on the left
  const spreadEl = wrapper.querySelector('.spread');
  if (!spreadEl) { return; }
  const rect = spreadEl.getBoundingClientRect();
  const wrapperRect = wrapper.getBoundingClientRect();
  // Place the flip page over the soon-to-be-gone page
  const pageW = rect.width / 2;
  flipPage.style.left = forward
    ? `${rect.left - wrapperRect.left + pageW}px`
    : `${rect.left - wrapperRect.left}px`;
  flipPage.style.top = `${rect.top - wrapperRect.top}px`;

  flipPage.innerHTML = `
    <div class="flip-face front"><img src="${window._pages[leavingIdx]}" alt=""></div>
    <div class="flip-face back"><img src="${window._pages[arrivingIdx]}" alt=""></div>
  `;
  wrapper.appendChild(flipPage);

  // Force a reflow so the transition fires
  void flipPage.offsetWidth;
  flipPage.classList.add(forward ? 'flipping-forward' : 'flipping-backward');

  // Clean up after the transition
  setTimeout(() => {
    if (flipPage.parentNode) flipPage.parentNode.removeChild(flipPage);
  }, 650);
}

function updateChrome() {
  // Compute the visible page range for the indicator. In book mode, a
  // spread shows two pages so we display "1–2 / 7"; the cover spread on a
  // book shows just "1 / 7" (cover alone).
  function pageRangeText() {
    if (!bookMode) {
      return `${currentPage + 1} / ${totalPages}`;
    }
    const [left, right] = spreadPagesFor(currentPage);
    const visible = [left, right].filter(x => x !== null);
    if (visible.length === 1) {
      return `${visible[0] + 1} / ${totalPages}`;
    }
    return `${visible[0] + 1}–${visible[1] + 1} / ${totalPages}`;
  }

  // The "next" button should be disabled when we're already on the last
  // spread. Determining "last spread" depends on whether we have a solo
  // cover. Easier: check whether goPage(+1) would actually advance.
  function isAtFirstSpread() {
    return currentPage === 0;
  }
  function isAtLastSpread() {
    if (!bookMode) return currentPage >= totalPages - 1;
    const [left, right] = spreadPagesFor(currentPage);
    const lastVisible = right !== null ? right : left;
    return lastVisible >= totalPages - 1;
  }

  // Top toolbar
  const ind = document.getElementById('page-indicator');
  if (ind) ind.textContent = pageRangeText();
  const prev = document.getElementById('btn-prev');
  const next = document.getElementById('btn-next');
  if (prev) prev.disabled = isAtFirstSpread();
  if (next) next.disabled = isAtLastSpread();

  // Zen-mode floating controls
  const zenInd = document.getElementById('zen-indicator');
  if (zenInd) zenInd.textContent = pageRangeText();
  const zenPrev = document.getElementById('zen-prev');
  const zenNext = document.getElementById('zen-next');
  if (zenPrev) zenPrev.disabled = isAtFirstSpread();
  if (zenNext) zenNext.disabled = isAtLastSpread();

  // Thumb-strip active state. In book mode a spread shows up to two pages,
  // so both of them get the .active class (the "single page vibe" — every
  // visible page is highlighted in the strip, not just the spread's start).
  const visiblePages = bookMode
    ? spreadPagesFor(currentPage).filter(x => x !== null)
    : [currentPage];
  const visibleSet = new Set(visiblePages);
  document.querySelectorAll('.thumb').forEach((el, i) => {
    el.classList.toggle('active', visibleSet.has(i));
  });
  // Scroll the leftmost visible page into view so we don't keep yanking
  // the strip when navigating forward across spreads.
  const scrollTarget = visiblePages[0];
  const t = document.getElementById(`thumb-${scrollTarget}`);
  if (t) t.scrollIntoView({behavior:'smooth', inline:'center', block:'nearest'});
}

function goPage(delta) {
  // In single-page mode, just step by 1.
  if (!bookMode) {
    showPage(currentPage + delta);
    return;
  }

  // In book mode, navigation moves to the adjacent SPREAD. The increment
  // depends on whether this item has a solo cover.
  if (isCoverOnRight()) {
    // Books: cover (0) is alone on the right, then [1,2], [3,4]…
    if (delta > 0) {
      if (currentPage === 0) showPage(1);
      else showPage(currentPage + 2);
    } else {
      if (currentPage <= 2) showPage(0);
      else showPage(currentPage - 2);
    }
  } else {
    // Notebooks: paired layout from page 0, no solo cover.
    if (delta > 0) {
      showPage(currentPage + 2);
    } else {
      showPage(Math.max(0, currentPage - 2));
    }
  }
}

function jumpPage(n) {
  // Thumbnail click: if book mode is active, snap to the containing spread
  showPage(n);
}

function changeZoom(d) { zoom = Math.max(0.3, Math.min(3.0, zoom + d)); applyZoom(); }
function resetZoom() { zoom = 1.0; applyZoom(); }
function applyZoom() {
  const w = document.getElementById('page-wrapper');
  const l = document.getElementById('zoom-label');
  if (w) w.style.transform = `scale(${zoom})`;
  if (l) l.textContent = Math.round(zoom * 100) + '%';
  // Also update the zen-mode zoom indicator if it exists
  const zenLabel = document.getElementById('zen-zoom-label');
  if (zenLabel) zenLabel.textContent = Math.round(zoom * 100) + '%';
}

function showPdfFallback(it, data) {
  const reader = document.getElementById('reader');
  reader.innerHTML = `
    <div class="reader-toolbar">
      <div class="reader-title">
        ${it.kind.startsWith('cloud_') ? `<span>${formatName(it)}</span>`
          : `<span style="cursor:pointer" title="Click to rename"
              onclick="renameItem('${it.id}')">${formatName(it)}</span>`}
        <em style="margin-left:8px">· PDF</em>
        ${it.kind === 'document' && it.annotated_pages
          ? `<span class="annotation-pill ok" title="Pages with handwriting baked into this PDF"><span>✏</span> ${it.annotated_pages} annotated page${it.annotated_pages!==1?'s':''}</span>`
          : ''}
      </div>
      <a class="btn-icon" style="color:var(--text);border-color:var(--paper-border)"
         href="${data.pdf_url}" target="_blank" rel="noopener">⤓ Download</a>
    </div>
    <iframe class="pdf-iframe" src="${data.pdf_url}#view=FitH"></iframe>
  `;
}

function showEmptyMessage(it) {
  document.getElementById('reader').innerHTML = `
    <div class="page-viewport">
      <div class="info-box">
        <strong>No pages found</strong>
        <em>${formatName(it)}</em> was processed, but no pages came out.
        It may be empty, or the format isn't fully supported yet.
      </div>
    </div>`;
}

function showError(it, msg) {
  document.getElementById('reader').innerHTML = `
    <div class="page-viewport">
      <div class="error-box">
        <strong>Could not open this item</strong>
        <pre style="white-space:pre-wrap;font-family:'DM Mono',monospace;font-size:0.75rem;line-height:1.5;margin:8px 0;color:#6e3030">${escHtml(msg)}</pre>
        Check the server console for the full traceback.
      </div>
    </div>`;
}

// ── Screenshot lightbox ──────────────────────────────────────────────────────
function showScreenshot(it) {
  const dateLabel = formatDateTime(it.captured_at || it.modified);
  const reader = document.getElementById('reader');
  reader.innerHTML = `
    <div class="lightbox" id="lightbox">
      <div class="lightbox-toolbar">
        <div class="lightbox-title">${dateLabel}</div>
        <span class="lightbox-meta">${escHtml(it.filename)} · ${it.size_kb}kb</span>
        <a class="btn-icon" style="color:var(--text);border-color:var(--paper-border)"
           href="/screenshot/${it.id}" download="${escHtml(it.filename)}">⤓ Download</a>
      </div>
      <div class="lightbox-image-wrap">
        <img class="lightbox-image" src="/screenshot/${it.id}" alt="">
      </div>
    </div>
  `;
}

function handleKey(e) {
  if (!currentItem || e.target.tagName === 'INPUT') return;
  if (document.getElementById('upload-modal').classList.contains('show')) return;
  if (e.key === 'ArrowLeft' || e.key === 'ArrowUp') goPage(-1);
  if (e.key === 'ArrowRight' || e.key === 'ArrowDown') goPage(1);
  if (e.key === '+' || e.key === '=') changeZoom(0.15);
  if (e.key === '-') changeZoom(-0.15);
  if (e.key === '0') resetZoom();
  if (e.key === 'z' || e.key === 'Z') toggleZen();
  if (e.key === 'b' || e.key === 'B') toggleBookMode();
  if (e.key === 'Escape' && document.body.classList.contains('zen-mode')) toggleZen();
}

// ── Zen mode ─────────────────────────────────────────────────────────────────
function toggleZen() {
  if (!currentItem) return;
  const wasZen = document.body.classList.toggle('zen-mode');
  if (wasZen) {
    showZenControlsTemporarily();
    bindZenHover();
  } else {
    unbindZenHover();
  }
  updateChrome();
  // The viewport size changes when entering/leaving zen mode (sidebar +
  // topbar appear/disappear). Re-fit the page dimensions to the new size.
  // setTimeout because the CSS transition needs a frame to settle first.
  setTimeout(() => resizePageContainers(), 50);
}

let _zenHideTimer = null;
let _zenHoverHandler = null;

function showZenControlsTemporarily() {
  document.body.classList.add('show-zen-controls');
  if (_zenHideTimer) clearTimeout(_zenHideTimer);
  _zenHideTimer = setTimeout(() => {
    document.body.classList.remove('show-zen-controls');
  }, 2200);
}

function bindZenHover() {
  if (_zenHoverHandler) return;
  _zenHoverHandler = () => showZenControlsTemporarily();
  document.addEventListener('mousemove', _zenHoverHandler);
}

function unbindZenHover() {
  if (_zenHoverHandler) {
    document.removeEventListener('mousemove', _zenHoverHandler);
    _zenHoverHandler = null;
  }
  document.body.classList.remove('show-zen-controls');
  if (_zenHideTimer) { clearTimeout(_zenHideTimer); _zenHideTimer = null; }
}

// ── Book mode ────────────────────────────────────────────────────────────────
function toggleBookMode() {
  if (!currentItem || totalPages < 3) return;
  bookMode = !bookMode;
  // Persist preference
  try { window.localStorage && window.localStorage.setItem('bookMode', bookMode ? '1' : ''); } catch(e){}
  // Re-render reader from current data without re-fetching
  document.body.classList.toggle('book-mode', bookMode);
  // Update toolbar button state
  const tgl = document.querySelector('.mode-toggle .btn-nav');
  if (tgl) tgl.classList.toggle('active', bookMode);
  // Snap currentPage to a valid spread start in book mode
  if (bookMode) currentPage = snapToSpreadStart(currentPage);
  renderPage();
  updateChrome();
}

// ── Sync ─────────────────────────────────────────────────────────────────────
// ── Upload ───────────────────────────────────────────────────────────────────
let uploadFiles = [];
let uploadTargets = null;

async function openUpload() {
  uploadFiles = [];
  document.getElementById('upload-input').value = '';
  document.getElementById('upload-status').innerHTML = '';
  document.getElementById('drop-zone-text').textContent = 'Drop files here or click to choose';
  document.getElementById('upload-modal').classList.add('show');
  try {
    uploadTargets = await (await fetch('/api/upload_targets')).json();
  } catch (e) { uploadTargets = null; }
  const stk = uploadTargets && uploadTargets.stk_available;
  const usb = uploadTargets && uploadTargets.device_connected;
  const stkInput = document.querySelector('input[name="upload-method"][value="stk"]');
  const usbInput = document.querySelector('input[name="upload-method"][value="usb"]');
  stkInput.disabled = !stk;
  usbInput.disabled = !usb;
  stkInput.closest('label').classList.toggle('disabled', !stk);
  usbInput.closest('label').classList.toggle('disabled', !usb);
  if (!stk) document.getElementById('stk-hint').textContent =
    'Send to Kindle for PC is not installed. Get it from amazon.com/sendtokindle.';
  if (!usb) document.getElementById('usb-hint').textContent =
    'Connect the Kindle by USB to use this option.';
  if (!stk && usb) usbInput.checked = true;
  if (stk && !usb) stkInput.checked = true;
  refreshUploadButton();
}

function closeUpload() {
  document.getElementById('upload-modal').classList.remove('show');
}

function uploadMethod() {
  const el = document.querySelector('input[name="upload-method"]:checked');
  return el && !el.disabled ? el.value : null;
}

function setUploadFiles(fileList) {
  uploadFiles = Array.from(fileList || []);
  const n = uploadFiles.length;
  document.getElementById('drop-zone-text').textContent = n === 0
    ? 'Drop files here or click to choose'
    : (n === 1 ? uploadFiles[0].name : `${n} files: ` + uploadFiles.map(f => f.name).join(', '));
  refreshUploadButton();
}

function refreshUploadButton() {
  document.getElementById('upload-go').disabled = !(uploadFiles.length && uploadMethod());
}

async function doUpload() {
  const method = uploadMethod();
  if (!method || !uploadFiles.length) return;
  const status = document.getElementById('upload-status');
  const btn = document.getElementById('upload-go');
  btn.disabled = true;
  status.innerHTML = method === 'usb'
    ? '<span class="info">Copying to the Kindle over USB…</span>'
    : '<span class="info">Handing the files to Send to Kindle…</span>';
  const form = new FormData();
  form.append('method', method);
  uploadFiles.forEach(f => form.append('files', f, f.name));
  try {
    const d = await (await fetch('/api/upload', { method: 'POST', body: form })).json();
    let html = '';
    (d.results || []).forEach(r => {
      html += r.ok
        ? `<span class="ok">✓ ${escHtml(r.file)}</span><br>`
        : `<span class="err">✗ ${escHtml(r.file)}: ${escHtml(r.error || 'failed')}</span><br>`;
    });
    if (d.error) html += `<span class="err">✗ ${escHtml(d.error)}</span><br>`;
    if (d.message) html += escHtml(d.message);
    status.innerHTML = html;
    if (d.ok) setUploadFiles([]);
  } catch (e) {
    status.innerHTML = `<span class="err">✗ Upload failed: ${escHtml(e.message)}</span>`;
  }
  refreshUploadButton();
}

(function initDropZone() {
  const zone = document.getElementById('drop-zone');
  ['dragenter', 'dragover'].forEach(ev => zone.addEventListener(ev, e => {
    e.preventDefault(); zone.classList.add('dragover');
  }));
  ['dragleave', 'drop'].forEach(ev => zone.addEventListener(ev, e => {
    e.preventDefault(); zone.classList.remove('dragover');
  }));
  zone.addEventListener('drop', e => setUploadFiles(e.dataTransfer.files));
  document.addEventListener('keydown', e => {
    if (e.key === 'Escape' && document.getElementById('upload-modal').classList.contains('show')) closeUpload();
  });
})();

async function syncAll() {
  const drawer = document.getElementById('sync-drawer');
  const log = document.getElementById('sync-log');
  drawer.style.display = 'block';
  log.innerHTML = '<span class="info">Syncing from device — first run can take a while…</span>\n';

  try {
    const r = await fetch('/api/sync', { method: 'POST' });
    const d = await r.json();
    if (!d.ok) {
      log.innerHTML += `<span class="err">✗ ${escHtml(d.error || 'Could not start sync')}</span>\n`;
      return;
    }
    pollSyncProgress();
  } catch(e) {
    log.innerHTML += `<span class="err">✗ Request failed: ${escHtml(e.message)}</span>\n`;
  }
}

function pollSyncProgress() {
  if (_syncTimer) clearInterval(_syncTimer);
  let ticks = 0;
  _syncTimer = setInterval(async () => {
    let s;
    try {
      s = await (await fetch('/api/sync_progress')).json();
    } catch(e) { return; }

    const track = document.getElementById('sync-progress-track');
    const fill = document.getElementById('sync-progress-fill');
    const msg = document.getElementById('sync-progress-msg');
    if (s.total > 0) {
      track.style.display = 'block';
      fill.style.width = Math.round(100 * s.current / Math.max(1, s.total)) + '%';
    }
    msg.textContent = s.message || '';

    // Refresh the tab counts and item list every few ticks so freshly
    // synced content appears while the sync is still running.
    if (s.running && ++ticks % 4 === 0) {
      loadLibrary();
      loadStatus();
    }

    if (!s.running) {
      clearInterval(_syncTimer);
      _syncTimer = null;
      const log = document.getElementById('sync-log');
      if (s.error) {
        log.innerHTML += `<span class="err">✗ ${escHtml(s.error)}</span>\n`;
      } else if (s.result) {
        fill.style.width = '100%';
        log.innerHTML += `<span class="ok">✓ ${s.result.synced_docs} document(s), ` +
          `${s.result.synced_notebooks} notebook(s), ${s.result.synced_screenshots} screenshot(s)</span>\n`;
      }
      msg.textContent = '';
      await loadLibrary();
      await loadStatus();
    }
  }, 800);
}

function hideSyncDrawer() {
  document.getElementById('sync-drawer').style.display = 'none';
}

async function renameItem(id, e) {
  if (e) e.stopPropagation();
  const it = [...library.documents, ...library.notebooks].find(n => n.id === id);
  if (!it) return;
  const hint = it.kind === 'notebook'
    ? 'Notebook name (empty to reset to UUID):'
    : 'Display name (empty to reset to the device name):';
  // Documents: offer the current name for editing (the device name is a
  // sensible starting point). Notebooks: a UUID is not, so start blank
  // unless a custom name is set.
  const initial = it.kind === 'notebook'
    ? (it.custom_name ? it.name : '')
    : it.name;
  const name = prompt(hint, initial);
  if (name === null) return;
  try {
    const r = await fetch(`/api/${it.kind}/${id}/rename`, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({name}),
    });
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    await loadLibrary();
    if (currentItem && currentItem.id === id) openItem(id);
  } catch (e) {
    alert('Rename failed: ' + e.message);
  }
}

// ── Misc ─────────────────────────────────────────────────────────────────────
async function clearCache() {
  await fetch('/api/clear_cache', { method: 'POST' });
  await loadStatus();
  if (currentItem) openItem(currentItem.id);
}

init();
</script>
</body>
</html>
"""


# ── CLI ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Scribe Library web UI")
    parser.add_argument("--staging", default=str(STAGING_ROOT),
                        help="Staging mirror directory (default: scribe_staging/)")
    parser.add_argument("--port", type=int, default=7070)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--sync", action="store_true",
                        help="Sync from the device before serving")
    parser.add_argument("--verbose", "-v", action="store_true")
    parser.add_argument("--stk-handler",
                        help="Path to Send to Kindle's StkSendToHandler.exe "
                             "(found automatically when installed normally)")
    args = parser.parse_args()
    _stk_override["path"] = args.stk_handler

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )

    STAGING_ROOT = Path(args.staging).expanduser()

    if args.sync:
        try:
            from scribe_mtp import sync_from_device
            print("\nSyncing from device...")
            r = sync_from_device(str(STAGING_ROOT),
                                 include_standalone_notebooks=True,
                                 include_screenshots=True)
            print(f"   {r['synced_docs']} documents, "
                  f"{r['synced_notebooks']} notebooks, "
                  f"{r['synced_screenshots']} screenshots")
        except Exception as e:
            print(f"   Sync failed: {e}")

    dep_msg = nbk_to_pdf._check_deps()
    if dep_msg:
        print("\nWARNING - decoder dependencies missing:")
        for line in dep_msg.splitlines():
            print(f"   {line}")

    start_device_watcher()
    start_issue_date_filler()
    _load_cloud_cache()

    doc_count = len(discover_document_items())
    nb_count = len(discover_notebook_items())
    ss_count = len(discover_screenshot_items())
    print(f"\nScribe Library")
    print(f"   Staging:  {STAGING_ROOT}")
    print(f"   Found:    {doc_count} documents, {nb_count} notebooks, "
          f"{ss_count} screenshots")
    print(f"   Open:     http://{args.host}:{args.port}\n")

    app.run(host=args.host, port=args.port, debug=False)
