"""
scribe_annotate.py — Export annotated documents and handwritten notebooks
from a Kindle Scribe.

Extracts the original PDF embedded in each document's KFX file, decodes the
handwritten stroke data from the matching Scribe notebook, maps every
annotation to its page via KFX position IDs from the .yjr sidecar, and
renders the strokes as transparent overlays. Standalone notebooks are
converted to PDFs of their own.

Requirements: see requirements.txt (kfxlib is vendored)

Usage:
  # Everything from a USB-connected Kindle Scribe: documents + notebooks
  python scribe_annotate.py --all

  # From a Scribe backup folder (device mirror or flat export folder)
  python scribe_annotate.py --scribe-dir "C:/Scribe export" --filter MDO

  # Standalone handwritten notebooks only
  python scribe_annotate.py --export-notebooks
"""
import sys, os, re, json, io, logging, shutil, zipfile, argparse, csv, time, threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import fitz  # PyMuPDF
import cairosvg
from tqdm import tqdm
from kfxlib.yj_book import YJ_Book
from kfxlib.message_logging import set_logger, JobLog

class TqdmLoggingHandler(logging.Handler):
    """Route log output through tqdm.write() to avoid progress bar corruption."""
    def emit(self, record):
        try:
            tqdm.write(self.format(record))
        except Exception:
            self.handleError(record)


class _KfxlibConsoleFilter(logging.Filter):
    """Suppress kfxlib INFO/DEBUG on console; WARNING/ERROR still pass through."""
    def filter(self, record):
        if record.name.startswith("kfxlib") and record.levelno <= logging.INFO:
            return False
        return True


_console_handler = TqdmLoggingHandler()
_console_handler.setFormatter(logging.Formatter('%(levelname)s: %(message)s'))
_console_handler.addFilter(_KfxlibConsoleFilter())
logging.root.addHandler(_console_handler)
logging.root.setLevel(logging.INFO)

log = logging.getLogger(__name__)


def setup_file_logging(output_dir):
    """Add a file handler so all log output is also written to a log file."""
    log_file = Path(output_dir) / 'scribe_annotate.log'
    fh = logging.FileHandler(str(log_file), mode='w', encoding='utf-8')
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter('%(asctime)s %(levelname)s: %(message)s',
                                      datefmt='%Y-%m-%d %H:%M:%S'))
    logging.getLogger().addHandler(fh)
    logging.root.setLevel(logging.DEBUG)
    _console_handler.setLevel(logging.INFO)
    log.info(f"Log file: {log_file}")


ASIN_RE = re.compile(r'_([A-F0-9]{32})')


# ---------------------------------------------------------------------------
# .yjr parsing
# ---------------------------------------------------------------------------

def parse_yjr(yjr_path):
    """Extract (pid_offset, note_id) pairs from .yjr binary (Amazon Ion)."""
    with open(yjr_path, 'rb') as f:
        raw = f.read()

    pos_pattern = rb'([A-Za-z0-9+/]{12,16}):(\d+)'
    note_pattern = rb'(c[A-Za-z0-9_-]{20,30})'
    positions = [(m.group(1).decode(), int(m.group(2).decode()), m.start())
                 for m in re.finditer(pos_pattern, raw)]
    notes = [(m.group(1).decode(), m.start())
             for m in re.finditer(note_pattern, raw)]

    entries = []
    i = 0
    while i < len(positions) - 1:
        p1_b64, p1_off, p1_start = positions[i]
        p2_b64, p2_off, p2_start = positions[i + 1]
        if p1_b64 == p2_b64 and p1_off == p2_off:
            for note_id, note_start in notes:
                if note_start > p2_start:
                    entries.append((p1_off, note_id))
                    break
            i += 2
        else:
            i += 1

    return entries


# ---------------------------------------------------------------------------
# PID-to-page mapping
# ---------------------------------------------------------------------------

def _matrix_rotation(m):
    """Rotation in degrees encoded by a 2D transform matrix (or 0)."""
    try:
        a, b, c, d = (float(v) for v in m[0:4])
    except (TypeError, ValueError, IndexError):
        return 0
    if (a, b, c, d) == (0.0, -1.0, 1.0, 0.0):
        return 90
    if (a, b, c, d) == (0.0, 1.0, -1.0, 0.0):
        return -90
    if (a, b, c, d) == (-1.0, 0.0, 0.0, -1.0):
        return 180
    return 0


def _find_rotation(value):
    """Recursively find the first rotating $98 transform in a fragment value."""
    if isinstance(value, dict):
        if '$98' in value:
            rot = _matrix_rotation(value['$98'])
            if rot:
                return rot
        for v in value.values():
            rot = _find_rotation(v)
            if rot:
                return rot
    elif isinstance(value, (list, tuple)):
        for v in value:
            rot = _find_rotation(v)
            if rot:
                return rot
    return 0


def _collect_page_rotations(book, section_names):
    """Per-page display rotation from the $98 transforms in storylines.

    The device shows such pages rotated (e.g. a sideways-stored crossword
    displayed upright); ink is recorded relative to the ROTATED view and
    must be stamped back with the same rotation.
    Returns {page_index: degrees}.
    """
    story_rotation = {}
    for frag in book.fragments:
        if str(frag.ftype) == '$259':
            rot = _find_rotation(frag.value)
            if rot:
                story_rotation[str(frag.fid)] = rot
    if not story_rotation:
        return {}

    rotations = {}
    for page_index, sn in enumerate(str(s) for s in section_names):
        for frag in book.fragments:
            if str(frag.ftype) == '$260' and str(frag.fid) == sn:
                blob = repr(frag.value)
                for story, rot in story_rotation.items():
                    if story in blob:
                        rotations[page_index] = rot
                        break
    return rotations


def build_pid_map_from_kfx(kfx_path):
    """Decode KFX to get exact section PID ranges.

    Skips kfxlib's check_consistency() which validates every embedded image
    with PIL — that takes ~30 min for large docs but is irrelevant for PID mapping.
    With the skip, processing takes ~5-10 seconds.
    """
    kfx_size_mb = os.path.getsize(kfx_path) / (1024 * 1024)
    log.info(f"Decoding KFX for PID map: {kfx_path} ({kfx_size_mb:.1f} MB)")

    job_log = JobLog(logging.getLogger("kfxlib.decode"))
    set_logger(job_log)

    pbar = tqdm(total=100, desc="Decoding KFX", unit="%", leave=False,
                bar_format='{l_bar}{bar}| {n_fmt}/{total_fmt}%')

    t0 = time.time()
    last_pct = [-1]

    def _progress(i, total):
        pct = 100 * i // total if total else 0
        if pct > last_pct[0]:
            pbar.update(pct - last_pct[0])
            last_pct[0] = pct

    book = YJ_Book(str(kfx_path))
    book.decode_book(pure=True, skip_consistency_check=True, progress_callback=_progress)

    pbar.update(100 - last_pct[0])
    pbar.close()

    t1 = time.time()
    log.info(f"KFX decode completed in {t1-t0:.1f}s")

    section_names = book.ordered_section_names()
    section_to_page = {str(sn): idx for idx, sn in enumerate(section_names)}

    pos_info = book.collect_content_position_info()
    log.info(f"PID map built ({len(pos_info)} content chunks)")

    section_pid_start = {}
    section_pid_end = {}
    for chunk in pos_info:
        sn = str(chunk.section_name) if chunk.section_name else ''
        if sn not in section_pid_start:
            section_pid_start[sn] = chunk.pid
        section_pid_end[sn] = chunk.pid + chunk.length

    boundaries = []
    for sn in [str(s) for s in section_names]:
        if sn in section_pid_start:
            boundaries.append((section_pid_start[sn], section_to_page[sn]))
    boundaries.sort()

    rotations = _collect_page_rotations(book, section_names)
    if rotations:
        log.info(f"Rotated pages: { {p: r for p, r in sorted(rotations.items())} }")

    log.info(f"Exact PID map: {len(boundaries)} pages, "
             f"PID range 0-{boundaries[-1][0] if boundaries else 0}")
    return boundaries, rotations


def build_pid_map(kfx_path=None, cache_path=None):
    """Build PID boundary list + page rotations.

    Uses the cache if available (and rotation-aware), otherwise decodes the
    KFX. Returns (boundaries, rotations) or (None, None).
    """
    if cache_path and os.path.exists(cache_path):
        with open(cache_path) as f:
            data = json.load(f)
        if isinstance(data, dict) and 'rotations' in data:
            boundaries = data['boundaries']
            rotations = {int(k): v for k, v in data['rotations'].items()}
            log.info(f"Loaded cached PID map ({len(boundaries)} pages)")
            return boundaries, rotations
        # pre-rotation cache format: rebuild so rotations get captured

    if not kfx_path or not os.path.exists(kfx_path):
        log.error(f"KFX file required for PID mapping but not found: {kfx_path}")
        log.error("Cannot map annotations without a KFX file. Provide the path to "
                   "the .kfx file or use --scribe-dir pointing to a Scribe backup.")
        return None, None

    boundaries, rotations = build_pid_map_from_kfx(kfx_path)

    if cache_path:
        os.makedirs(os.path.dirname(cache_path) or '.', exist_ok=True)
        with open(cache_path, 'w') as f:
            json.dump({'mode': 'exact', 'boundaries': boundaries,
                       'rotations': rotations}, f)
        log.info(f"Saved PID map cache: {cache_path}")

    return boundaries, rotations


def pid_to_page(pid, boundaries):
    """Binary search for the page containing the given PID."""
    lo, hi = 0, len(boundaries) - 1
    result = -1
    while lo <= hi:
        mid = (lo + hi) // 2
        if boundaries[mid][0] <= pid:
            result = boundaries[mid][1]
            lo = mid + 1
        else:
            hi = mid - 1
    return result


# ---------------------------------------------------------------------------
# Notebook decoding & rendering
# ---------------------------------------------------------------------------

def decode_notebook_to_svgs(notebook_dir):
    """Decode Kindle Scribe notebook to {note_id: svg_bytes} dict."""
    job_log = JobLog(logging.getLogger("kfxlib.notebook"))
    set_logger(job_log)

    book = YJ_Book(str(notebook_dir))
    epub_bytes = book.convert_to_epub()

    svgs = {}
    with zipfile.ZipFile(io.BytesIO(epub_bytes)) as zf:
        for name in zf.namelist():
            if name.endswith('.svg'):
                svgs[Path(name).stem] = zf.read(name)

    log.info(f"Decoded {len(svgs)} SVG annotation pages from notebook")
    return svgs


def render_svg_to_png(svg_bytes, width, height):
    """Render SVG to transparent PNG at given pixel dimensions."""
    return cairosvg.svg2png(
        bytestring=svg_bytes,
        output_width=int(width),
        output_height=int(height),
        background_color="transparent"
    )


def _svg_canvas_size(svg_bytes):
    """Width/height of the stroke canvas, from the SVG viewBox."""
    m = re.search(rb'viewBox="([\d.\s\-]+)"', svg_bytes)
    if m:
        parts = m.group(1).split()
        if len(parts) == 4:
            try:
                w, h = float(parts[2]), float(parts[3])
                if w > 0 and h > 0:
                    return w, h
            except ValueError:
                pass
    return 1860.0, 2480.0  # Scribe screen canvas


def _overlay_rect(page_rect, canvas_aspect):
    """Rect the stroke canvas must map onto so ink lands where it was written.

    The device displays a page fitted inside its screen (letterboxed when the
    aspect ratios differ) while strokes are recorded on the full screen
    canvas. Mapping the canvas straight onto the page rect therefore shifts
    ink towards the page edges on pages whose aspect differs from the screen.
    The correct target is the smallest canvas-aspect rect that covers the
    page, centered on it; ink outside the page is clipped.
    """
    w, h = page_rect.width, page_rect.height
    rw = max(w, h * canvas_aspect)
    rh = rw / canvas_aspect
    x0 = page_rect.x0 - (rw - w) / 2
    y0 = page_rect.y0 - (rh - h) / 2
    return fitz.Rect(x0, y0, x0 + rw, y0 + rh)


def _rotated_target_and_bbox(page_rect, rot, canvas_aspect):
    """Like _overlay_rect, but for a page the device displays ROTATED.

    Computes the covering rect in the ROTATED (as-displayed) orientation,
    then maps it back into raw page coordinates by swapping around the
    page's center - same construction as the non-rotated case, just with
    width/height swapped for the rotated view.

    The covering rect can extend beyond the page's own boundary in raw
    space (whenever the rotated page's aspect differs a lot from the
    screen's, e.g. a landscape crossword on a portrait screen - the extra
    canvas is where margin annotations get written). Content placed there
    would be silently clipped by the PDF's MediaBox regardless of the
    /Rotate flag, so the caller must enlarge the page to needed_bbox before
    stamping, or such annotations are unrecoverable.

    Returns (target_rect_in_raw_page_space, needed_page_bbox).
    """
    disp = fitz.Rect(0, 0, page_rect.height, page_rect.width)
    r = _overlay_rect(disp, canvas_aspect)
    cx = (page_rect.x0 + page_rect.x1) / 2
    cy = (page_rect.y0 + page_rect.y1) / 2
    target = fitz.Rect(cx - r.height / 2, cy - r.width / 2,
                       cx + r.height / 2, cy + r.width / 2)
    needed_bbox = fitz.Rect(page_rect) | target
    return target, needed_bbox


# ---------------------------------------------------------------------------
# Page title extraction (from PDF bookmarks)
# ---------------------------------------------------------------------------

def build_bookmark_titles(doc):
    """Build sorted [(page_index, title), ...] from PDF bookmarks/TOC."""
    toc = doc.get_toc()
    if not toc:
        return []
    return sorted([(entry[2] - 1, entry[1]) for entry in toc])


def get_page_title(page_index, bookmarks):
    """Look up the bookmark title for a page using binary search."""
    if not bookmarks:
        return f"Page {page_index + 1}"
    lo, hi = 0, len(bookmarks) - 1
    result = None
    while lo <= hi:
        mid = (lo + hi) // 2
        if bookmarks[mid][0] <= page_index:
            result = bookmarks[mid][1]
            lo = mid + 1
        else:
            hi = mid - 1
    return result if result else f"Page {page_index + 1}"


# ---------------------------------------------------------------------------
# PDF reconstruction from KFX
# ---------------------------------------------------------------------------

def _normalize_pdf(pdf_bytes):
    """Rewrite a PDF through pikepdf (qpdf) to repair nonstandard structure.

    The PDFs embedded in KFX can carry broken xref offsets that PyMuPDF
    reads fine but cannot re-save - the annotation overlay would then
    silently produce a corrupt file. qpdf rebuilds the xref in seconds.
    """
    try:
        import pikepdf
    except ImportError:
        log.warning("pikepdf not installed - using embedded PDF as-is")
        return pdf_bytes

    try:
        dst = io.BytesIO()
        with pikepdf.open(io.BytesIO(pdf_bytes)) as pdf:
            pdf.save(dst)
        return dst.getvalue()
    except Exception as e:
        log.warning(f"PDF normalization failed ({e}) - using embedded PDF as-is")
        return pdf_bytes


def _extract_single_embedded_pdf(book):
    """Fast path: Send-to-Kindle usually stores the converted document as ONE
    embedded copy of the original PDF, referenced once per page. In that case
    the original can be returned directly - instant and lossless.

    kfxlib's generic converter handles this case too, but post-processes the
    result with pypdf (metadata/outline injection), which takes hours on
    large link-heavy documents (e.g. 1800-page planners). The embedded
    original already carries its own links and bookmarks, so nothing is lost
    by skipping that.

    Returns PDF bytes, or None when the KFX doesn't have this shape
    (multiple fragments, image-backed pages, reflowable layout, ...).
    """
    from kfxlib.yj_to_image_book import KFX_IMAGE_BOOK

    try:
        resources, _, _ = book.get_ordered_image_resources()
        image_book = KFX_IMAGE_BOOK(book)

        locations = set()
        raw_media = None
        for fid in resources:
            r = image_book.get_resource_image(fid)
            if r is None or r.format != '$565':
                return None
            locations.add(r.location)
            raw_media = r.raw_media
        if len(locations) != 1:
            return None

        with fitz.open('pdf', raw_media) as doc:
            if len(doc) != len(resources):
                return None

        log.info(f"KFX embeds the original PDF ({len(resources)} pages) - extracting directly")
        return _normalize_pdf(raw_media)
    except Exception as e:
        log.debug(f"Embedded-PDF fast path not applicable: {e}")
        return None


def reconstruct_pdf_from_kfx(kfx_path):
    """Reconstruct a PDF from a KFX file.

    Tries the single-embedded-PDF fast path first (see
    _extract_single_embedded_pdf), then falls back to kfxlib's image-book
    converter. Pre-decodes with skip_consistency_check=True to avoid the
    slow PIL image validation pass (~30 min for large documents).

    Returns PDF bytes. Raises if the KFX can't be converted (e.g. text-heavy
    documents where kfxlib's convert_to_pdf() returns nothing).
    """
    job_log = JobLog(logging.getLogger("kfxlib.reconstruct"))
    set_logger(job_log)
    try:
        book = YJ_Book(str(kfx_path))
        book.decode_book(pure=True, skip_consistency_check=True)
        pdf_bytes = _extract_single_embedded_pdf(book)
        if pdf_bytes is None:
            try:
                pdf_bytes = book.convert_to_pdf()
            except Exception as e:
                log.debug(f"kfxlib conversion failed: {e}")
                pdf_bytes = None
    finally:
        set_logger(None)

    if not pdf_bytes:
        raise RuntimeError("This document's format is not supported for export.")
    return pdf_bytes


# ---------------------------------------------------------------------------
# Main overlay pipeline
# ---------------------------------------------------------------------------

def overlay_annotations(pdf_path, yjr_path, notebook_dir, output_path,
                        kfx_path=None, cache_path=None,
                        save_annotations_dir=None, progress_callback=None):
    """Main pipeline: parse .yjr, build PID map, decode notebook, overlay on PDF.

    Returns list of annotation records for logging.
    """
    log.info(f"PDF:      {pdf_path}")
    log.info(f"YJR:      {yjr_path}")
    log.info(f"Notebook: {notebook_dir}")

    annotations = parse_yjr(yjr_path)
    log.info(f"Found {len(annotations)} annotation entries")
    if not annotations:
        log.warning("No annotations found in .yjr, copying PDF as-is")
        shutil.copy2(pdf_path, output_path)
        return []

    boundaries, rotations = build_pid_map(kfx_path, cache_path)
    if boundaries is None:
        log.error("Cannot proceed without PID map")
        return []
    rotations = rotations or {}

    svgs = decode_notebook_to_svgs(notebook_dir)

    doc = fitz.open(str(pdf_path))
    total_pages = len(doc)
    bookmarks = build_bookmark_titles(doc)
    records = []

    if save_annotations_dir:
        os.makedirs(save_annotations_dir, exist_ok=True)
        log.info(f"Saving hi-res annotation PNGs to: {save_annotations_dir}")

    # Any page whose aspect ratio differs a lot from the device screen's
    # (rotated or not - e.g. a landscape character sheet on the portrait
    # screen) may need a larger MediaBox to avoid clipping ink written in
    # the device's extra canvas space beyond the printed page. Stamping
    # must happen BEFORE the page is enlarged: PyMuPDF's rotation transform
    # depends on the page's current MediaBox, and the target math here is
    # calibrated against the ORIGINAL (small) box. Track each page's
    # original rect and the largest bbox its annotations need; boxes are
    # grown once, after all stamping, right before rotated pages are
    # flagged for display.
    orig_page_rect = {}
    needed_bbox_by_page = {}

    sorted_annotations = sorted(annotations)
    for ann_num, (pid_offset, note_id) in enumerate(
            tqdm(sorted_annotations, desc="Overlaying annotations",
                 leave=False,
                 bar_format='{l_bar}{bar}| {n_fmt}/{total_fmt}'), 1):
        if progress_callback:
            progress_callback(ann_num, len(sorted_annotations))
        page_index = pid_to_page(pid_offset, boundaries)
        if page_index < 0 or page_index >= total_pages:
            log.warning(f"PID {pid_offset} -> page {page_index} (out of range), skipping")
            continue
        if note_id not in svgs:
            log.warning(f"SVG not found for note {note_id}, skipping")
            continue

        page = doc[page_index]
        title = get_page_title(page_index, bookmarks)
        svg_data = svgs[note_id]

        # Map the stroke canvas onto the page respecting the aspect ratio and
        # rotation the device displayed the page at (see _overlay_rect /
        # _rotated_target_and_bbox / _collect_page_rotations).
        rot = rotations.get(page_index, 0)
        sw, sh = _svg_canvas_size(svg_data)
        if page_index not in orig_page_rect:
            orig_page_rect[page_index] = fitz.Rect(page.rect)
        if rot in (90, -90):
            target, needed_bbox = _rotated_target_and_bbox(
                orig_page_rect[page_index], rot, sw / sh)
        else:
            target = _overlay_rect(orig_page_rect[page_index], sw / sh)
            needed_bbox = orig_page_rect[page_index] | target
        needed_bbox_by_page[page_index] = (
            needed_bbox | needed_bbox_by_page[page_index]
            if page_index in needed_bbox_by_page else needed_bbox)

        # Overlay the strokes as vector content (crisp at any zoom, small
        # output). Raster is only the fallback for SVGs cairosvg can't
        # convert to PDF.
        try:
            stroke_pdf = cairosvg.svg2pdf(bytestring=svg_data)
            with fitz.open('pdf', stroke_pdf) as stroke_doc:
                page.show_pdf_page(target, stroke_doc, 0, overlay=True,
                                   keep_proportion=False, rotate=rot)
        except Exception as e:
            log.warning(f"Vector overlay failed for note {note_id} ({e}) - using raster")
            png_data = render_svg_to_png(svg_data, target.width * 2,
                                         target.height * 2)
            page.insert_image(target, stream=png_data, overlay=True,
                              rotate=rot % 360)

        log.debug(f"  PID {pid_offset:>6} -> page {page_index+1:>4}: {title[:70]}")

        record = {
            'page_index': page_index,
            'page_number': page_index + 1,
            'page_title': title,
            'pid': pid_offset,
            'note_id': note_id,
        }
        records.append(record)

        if save_annotations_dir:
            hires_png = render_svg_to_png(svg_data, page.rect.width * 4, page.rect.height * 4)
            png_name = f"p{page_index+1:04d}_{note_id}.png"
            with open(os.path.join(save_annotations_dir, png_name), 'wb') as f:
                f.write(hires_png)

    # Grow any page whose annotations needed more room than its original
    # MediaBox (aspect mismatch against the device screen, rotated or not).
    # This happens here - AFTER all stamping - so the ink already baked in
    # at its correct (proven) coordinates simply becomes visible; growing
    # the box earlier would change the transform used while stamping
    # (verified empirically for the rotated case).
    #
    # set_mediabox() alone is enough: PyMuPDF automatically resets the
    # CropBox to match the new MediaBox (even if a distinct CropBox existed
    # before). Calling set_cropbox() explicitly with our own rect is not
    # only redundant but crashes ("CropBox not in MediaBox") whenever the
    # box has a negative origin - a PyMuPDF quirk with how it normalizes
    # such boxes internally (verified: page.mediabox read back immediately
    # after set_mediabox still fails the same way).
    for page_index, needed_bbox in needed_bbox_by_page.items():
        if 0 <= page_index < total_pages and needed_bbox != doc[page_index].rect:
            doc[page_index].set_mediabox(needed_bbox)

    # Pages the device displayed rotated: rotate them in the output too, so
    # the export reads the same way the user saw (and wrote on) them.
    for page_index, rot in rotations.items():
        if 0 <= page_index < total_pages:
            doc[page_index].set_rotation((doc[page_index].rotation + rot) % 360)

    log.info(f"Overlaid {len(records)}/{len(annotations)} annotations")

    log.info(f"Saving PDF ({total_pages} pages)...")
    t_save = time.time()
    doc.save(str(output_path), garbage=4, deflate=True)
    doc.close()
    save_time = time.time() - t_save
    size_mb = os.path.getsize(str(output_path)) / (1024 * 1024)
    log.info(f"Saved: {output_path} ({size_mb:.1f} MB, {save_time:.1f}s)")
    return records


# ---------------------------------------------------------------------------
# Log file generation
# ---------------------------------------------------------------------------

def write_log(all_records, log_path):
    """Write a structured log of all annotated pages across all documents."""
    with open(log_path, 'w', encoding='utf-8', newline='') as f:
        writer = csv.writer(f, delimiter='\t')
        writer.writerow(['Document', 'Page (Acrobat)', 'Page (0-indexed)', 'PID', 'Note ID', 'Page Title'])
        for doc_name, records in all_records:
            for r in records:
                writer.writerow([
                    doc_name,
                    r['page_number'],
                    r['page_index'],
                    r['pid'],
                    r['note_id'],
                    r['page_title'],
                ])
    log.info(f"Log written: {log_path}")

    json_path = str(log_path).rsplit('.', 1)[0] + '.json'
    json_data = []
    for doc_name, records in all_records:
        json_data.append({
            'document': doc_name,
            'annotation_count': len(records),
            'annotations': records,
        })
    with open(json_path, 'w', encoding='utf-8') as f:
        json.dump(json_data, f, indent=2, ensure_ascii=False)
    log.info(f"JSON log written: {json_path}")


# ---------------------------------------------------------------------------
# Auto-discovery
# ---------------------------------------------------------------------------

def extract_asin(path_str):
    """Extract 32-char hex ASIN from a path string."""
    m = ASIN_RE.search(str(path_str))
    return m.group(1) if m else None


def find_kfx_for_asin(search_dirs, asin):
    """Find a .kfx file for a given ASIN by searching recursively in multiple dirs."""
    for search_dir in search_dirs:
        search_path = Path(search_dir)
        if not search_path.exists():
            continue
        for kfx in search_path.rglob(f'*{asin}*.kfx'):
            return kfx
    return None


def discover_documents(yjr_dir, notebooks_dir, name_filter=None):
    """Auto-discover annotated documents.

    The yjr_dir is searched recursively for .sdr/ folders with .yjr files.
    KFX files are searched for next to the .sdr folder (same parent directory).

    Args:
        yjr_dir: Directory containing .sdr/ folders (searched recursively)
        notebooks_dir: Directory containing {ASIN}!!PDOC!!notebook/ folders
        name_filter: Only process documents whose name contains this string

    Returns list of dicts: name, yjr, notebook, asin, kfx_path, cache_path
    """
    sdr_base = Path(yjr_dir)
    notebooks_base = Path(notebooks_dir)

    if not sdr_base.exists():
        log.error(f"YJR/SDR directory not found: {sdr_base}")
        return []

    if not notebooks_base.exists():
        log.error(f"Notebooks directory not found: {notebooks_base}")
        return []

    results = []
    for sdr_dir in sorted(sdr_base.rglob('*.sdr')):
        yjr_files = list(sdr_dir.glob('*.yjr'))
        if not yjr_files:
            continue

        yjr_path = yjr_files[0]
        asin = extract_asin(sdr_dir.name)
        if not asin:
            continue

        doc_name = re.sub(r'(\.pdf)?_[A-F0-9]{32}\.sdr$', '', sdr_dir.name, flags=re.IGNORECASE)

        if name_filter and name_filter.lower() not in doc_name.lower():
            continue

        nb_dir = notebooks_base / f"{asin}!!PDOC!!notebook"
        if not nb_dir.exists():
            log.warning(f"Notebook not found for {doc_name} (ASIN {asin}), skipping")
            continue

        kfx_path = find_kfx_for_asin([sdr_dir.parent], asin)
        if not kfx_path:
            log.warning(f"No KFX file for '{doc_name}', skipping")
            continue

        cache_dir = Path(__file__).parent / 'cache'
        results.append({
            'name': doc_name,
            'yjr': yjr_path,
            'notebook': nb_dir,
            'asin': asin,
            'kfx_path': str(kfx_path),
            'cache_path': str(cache_dir / f"{asin}.pidmap.json"),
        })

    return results


# ---------------------------------------------------------------------------
# Standalone notebook discovery & export
# ---------------------------------------------------------------------------

def discover_standalone_notebooks(notebooks_dir, name_filter=None):
    """Find standalone handwritten notebooks (UUID-named dirs, not PDOC overlays).

    Returns list of dicts: name, path, nbk_path
    """
    notebooks_base = Path(notebooks_dir)
    if not notebooks_base.exists():
        log.error(f"Notebooks directory not found: {notebooks_base}")
        return []

    uuid_re = re.compile(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')
    results = []

    for entry in sorted(notebooks_base.iterdir()):
        if not entry.is_dir():
            continue
        if '!!PDOC!!' in entry.name:
            continue
        if not uuid_re.match(entry.name):
            continue

        nbk_file = entry / 'nbk'
        if not nbk_file.exists():
            continue

        # Try to extract a human-readable name from metadata, fall back to UUID
        notebook_name = _get_notebook_name(entry) or entry.name

        if name_filter and name_filter.lower() not in notebook_name.lower():
            continue

        results.append({
            'name': notebook_name,
            'path': entry,
            'nbk_path': nbk_file,
            'uuid': entry.name,
        })

    return results


def _get_notebook_name(notebook_dir):
    """Try to read the notebook's display name from its metadata."""
    meta_file = notebook_dir / 'metadata.json'
    if meta_file.exists():
        try:
            with open(meta_file, encoding='utf-8') as f:
                meta = json.load(f)
            return meta.get('title') or meta.get('name')
        except (json.JSONDecodeError, OSError):
            pass
    # kfxlib sometimes stores title inside the KDF itself — not worth
    # parsing here since nbk_to_pdf will handle it
    return None


def run_notebook_export(notebooks_dir, name_filter, output_dir):
    """Discover standalone notebooks and export them to <output_dir>/notebooks.

    Returns (ok_count, failed_count).
    """
    notebooks = discover_standalone_notebooks(notebooks_dir, name_filter)
    if not notebooks:
        log.error("No standalone notebooks found. Check paths and --filter.")
        return 0, 0

    log.info(f"Found {len(notebooks)} standalone notebook(s)")
    results = export_notebooks(notebooks, Path(output_dir) / 'notebooks')
    ok = sum(1 for r in results if r['status'] == 'ok')
    return ok, len(results) - ok


def export_notebooks(notebooks, output_dir):
    """Export standalone notebooks to PDF using nbk_to_pdf."""
    from nbk_to_pdf import convert_or_raise, ConversionError

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    results = []
    for nb in tqdm(notebooks, desc="Exporting notebooks",
                    bar_format='{l_bar}{bar}| {n_fmt}/{total_fmt} [{elapsed}, ~{remaining} left]'):
        safe_name = re.sub(r'[<>:"/\\|?*]', '_', nb['name'])
        out_path = output_dir / f"{safe_name}.pdf"

        log.info(f"\n{'='*60}")
        log.info(f"Exporting: {nb['name']} ({nb['uuid']})")

        try:
            stats = convert_or_raise(nb['nbk_path'], out_path)
            log.info(f"  -> {out_path} ({stats['pages']} pages)")
            results.append({
                'name': nb['name'],
                'uuid': nb['uuid'],
                'output': str(out_path),
                'pages': stats['pages'],
                'status': 'ok',
            })
        except ConversionError as e:
            log.error(f"  Failed: {e}")
            results.append({
                'name': nb['name'],
                'uuid': nb['uuid'],
                'status': 'error',
                'error': str(e),
            })

    ok = sum(1 for r in results if r['status'] == 'ok')
    log.info(f"\nExported {ok}/{len(results)} notebooks to {output_dir}")
    return results


# ---------------------------------------------------------------------------
# Source folder resolution
# ---------------------------------------------------------------------------

def resolve_scribe_dir(scribe_dir):
    """Resolve a Scribe backup root into component directories.

    Preferred layout is a mirror of the device's Internal Storage:
        <root>/.notebooks/            — notebook data (PDOC and standalone)
        <root>/documents/             — .sdr folders and .kfx files (any depth)

    But nothing is required: for a loose export (everything dumped into one
    folder — .sdr dirs, .kfx files, notebook folders side by side), the root
    itself is used and content is found by name patterns.

    Returns (yjr_dir, notebooks_dir).
    """
    root = Path(scribe_dir)
    if not root.exists():
        raise FileNotFoundError(f"Directory not found: {scribe_dir}")

    if not (root / '.notebooks').exists() and (root / 'scribe format' / '.notebooks').exists():
        root = root / 'scribe format'

    notebooks_dir = root / '.notebooks'
    docs_dir = root / 'documents'

    yjr_dir = docs_dir if docs_dir.exists() else root
    nb_dir = notebooks_dir if notebooks_dir.exists() else root
    if yjr_dir == root or nb_dir == root:
        log.info(f"No full device structure under {root} - treating it as a "
                 "flat folder (content found by name patterns)")

    return str(yjr_dir), str(nb_dir)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description='Export annotated documents and handwritten notebooks from a Kindle Scribe.',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""Examples:
  # Full export from a USB-connected Kindle Scribe: documents + notebooks
  python scribe_annotate.py --all

  # Annotated documents only, filtered by name (USB default)
  python scribe_annotate.py --filter MDO

  # From a local backup folder instead of USB (device mirror or flat folder)
  python scribe_annotate.py --scribe-dir "C:/Scribe backup" --filter MDO

  # Standalone handwritten notebooks only
  python scribe_annotate.py --export-notebooks
""")

    parser.add_argument('--scribe-dir',
                        help='Read from a local folder instead of USB: a mirror of the '
                             'device storage, or a flat folder holding .sdr folders, '
                             '.kfx files and notebook folders side by side.')
    parser.add_argument('--filter',
                        help='Only process documents whose name contains this string.')
    parser.add_argument('--all', action='store_true',
                        help='Export everything: annotated documents AND standalone notebooks.')
    parser.add_argument('--export-notebooks', action='store_true',
                        help='Export standalone handwritten notebooks only.')
    parser.add_argument('--output', default='output',
                        help='Output directory (default: output/).')
    parser.add_argument('--save-annotations', action='store_true',
                        help='Save individual annotation PNGs alongside PDFs.')
    parser.add_argument('--staging-dir', default='scribe_staging',
                        help=argparse.SUPPRESS)
    parser.add_argument('--quiet', action='store_true',
                        help='Suppress info-level logging.')
    args = parser.parse_args()

    if args.quiet:
        _console_handler.setLevel(logging.WARNING)

    # --- Resolve the source: local folder, or USB sync into staging ---
    if args.scribe_dir:
        try:
            yjr_dir, notebooks_dir = resolve_scribe_dir(args.scribe_dir)
        except FileNotFoundError as e:
            log.error(str(e))
            sys.exit(1)
    else:
        try:
            from scribe_mtp import sync_from_device
        except ImportError:
            log.error("USB mode requires win32com (pywin32). Install with: pip install pywin32")
            sys.exit(1)

        log.info("Syncing from USB-connected Kindle Scribe...")
        try:
            sync_result = sync_from_device(
                args.staging_dir,
                name_filter=args.filter if not args.export_notebooks else None,
                include_standalone_notebooks=args.export_notebooks or args.all,
            )
        except RuntimeError as e:
            log.error(str(e))
            sys.exit(1)

        log.info(f"Synced {sync_result['synced_docs']} document(s), "
                 f"{sync_result['synced_notebooks']} notebook(s)")
        yjr_dir = sync_result['yjr_dir']
        notebooks_dir = sync_result['notebooks_dir']

    # --- Standalone notebook export mode (notebooks only) ---
    if args.export_notebooks:
        output_dir = Path(args.output)
        output_dir.mkdir(parents=True, exist_ok=True)
        setup_file_logging(output_dir)

        ok, failed = run_notebook_export(notebooks_dir, args.filter, output_dir)
        log.info(f"\nDone. {ok} exported, {failed} failed.")
        sys.exit(0 if failed == 0 else 1)

    # --- Annotated documents (+ notebooks with --all) ---
    docs = discover_documents(yjr_dir, notebooks_dir, name_filter=args.filter)
    if not docs:
        if not args.all:
            log.error("No annotated documents found. Check paths and --filter.")
            sys.exit(1)
        log.warning("No annotated documents found - continuing with notebooks.")

    output_dir = Path(args.output)
    annotated_dir = output_dir / 'annotated'
    reconstructed_dir = output_dir / 'reconstructed'
    annotated_dir.mkdir(parents=True, exist_ok=True)
    setup_file_logging(output_dir)
    log.info(f"Found {len(docs)} annotated document(s)")

    all_records = []
    outer_bar = tqdm(docs, desc="Overall progress",
                     bar_format='{l_bar}{bar}| {n_fmt}/{total_fmt} [{elapsed}, ~{remaining} left]')
    _ticker_stop = threading.Event()
    def _ticker():
        while not _ticker_stop.wait(1):
            outer_bar.refresh()
    threading.Thread(target=_ticker, daemon=True).start()

    up_to_date = 0
    for doc in outer_bar:
        log.info(f"\n{'='*60}")
        log.info(f"Processing: {doc['name']}")
        out_path = annotated_dir / f"{doc['name']}.pdf"

        if not parse_yjr(str(doc['yjr'])):
            log.info("No annotations - skipping")
            continue

        # Incremental: skip documents whose export is newer than their
        # annotation sidecar (no new ink since the last run).
        if out_path.exists() and out_path.stat().st_mtime >= Path(doc['yjr']).stat().st_mtime:
            log.info("Up to date - skipping (no new annotations since last export)")
            up_to_date += 1
            continue

        reconstructed_dir.mkdir(parents=True, exist_ok=True)
        pdf_path = reconstructed_dir / f"{doc['name']}.pdf"
        if not pdf_path.exists():
            try:
                pdf_bytes = reconstruct_pdf_from_kfx(doc['kfx_path'])
                pdf_path.write_bytes(pdf_bytes)
                size_mb = len(pdf_bytes) / (1024 * 1024)
                log.info(f"Extracted base PDF: {pdf_path} ({size_mb:.1f} MB)")
            except Exception as e:
                log.error(f"Base PDF extraction failed: {e}")
                continue

        ann_dir = None
        if args.save_annotations:
            ann_dir = str(annotated_dir / f"{doc['name']} - annotations")

        try:
            records = overlay_annotations(
                str(pdf_path), str(doc['yjr']), str(doc['notebook']), str(out_path),
                kfx_path=doc['kfx_path'], cache_path=doc['cache_path'],
                save_annotations_dir=ann_dir)
            all_records.append((doc['name'], records))
        except Exception as e:
            log.error(f"Failed: {e}")
            import traceback
            traceback.print_exc()

    _ticker_stop.set()

    total = sum(len(r) for _, r in all_records)
    log.info(f"\nDone. {len(all_records)} document(s), {total} annotations overlaid"
             + (f", {up_to_date} already up to date." if up_to_date else "."))

    log_path = str(output_dir / 'annotations.log.tsv')
    write_log(all_records, log_path)

    # --all: also export standalone notebooks (--filter applies to
    # documents only; notebooks have UUID names)
    if args.all:
        log.info(f"\n{'='*60}")
        nb_ok, nb_failed = run_notebook_export(notebooks_dir, None, output_dir)
        log.info(f"Notebooks: {nb_ok} exported, {nb_failed} failed.")


if __name__ == '__main__':
    main()
