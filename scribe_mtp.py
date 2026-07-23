"""
scribe_mtp.py — Sync annotation data from a USB-connected Kindle Scribe via
Windows Shell COM (MTP).

Copies the needed files (.sdr, .kfx, PDOC notebooks, standalone notebooks)
from the device to a local staging directory, mirroring the device structure.

Uses Shell.Application COM API — works with the native Windows MTP driver,
no libusb or pyusb needed.
"""

import logging
import os
import re
import time
from pathlib import Path

log = logging.getLogger(__name__)

ASIN_RE = re.compile(r'_([A-F0-9]{32})')
UUID_RE = re.compile(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')

COPY_FLAGS = 16 | 4 | 1024  # yes-to-all, no progress dialog, no error UI


def _get_shell():
    import win32com.client
    return win32com.client.Dispatch('Shell.Application')


def find_kindle_scribe():
    """Find a connected Kindle Scribe. Returns (shell_item, name) or (None, None)."""
    shell = _get_shell()
    this_pc = shell.NameSpace(17)
    for item in this_pc.Items():
        name = item.Name.lower()
        if 'kindle' in name and 'scribe' in name:
            return item, item.Name
    for item in this_pc.Items():
        if 'kindle' in item.Name.lower():
            return item, item.Name
    return None, None


def _find_child(folder, name):
    """Find a child item by exact name."""
    for item in folder.Items():
        if item.Name == name:
            return item
    return None


def _copy_item_to(shell, item, dest_dir, timeout=120):
    """Copy a Shell item (file or folder) to a local directory via MTP.
    Returns True on success.
    """
    dest_dir = os.path.abspath(dest_dir)
    os.makedirs(dest_dir, exist_ok=True)
    dest_path = os.path.join(dest_dir, item.Name)

    # Skip if already exists with content
    if item.IsFolder:
        if os.path.isdir(dest_path) and os.listdir(dest_path):
            return True
    else:
        if os.path.isfile(dest_path) and os.path.getsize(dest_path) > 0:
            return True

    # Shell CopyHere silently fails when the destination path exceeds
    # Windows MAX_PATH (260 chars) — long document names hit this easily.
    # Stage such files via a short temp directory, then move them into
    # place with long-path-aware Python file operations.
    if not item.IsFolder and len(dest_path) >= 250:
        return _copy_file_via_short_dir(shell, item, dest_path, timeout)

    dest_ns = shell.NameSpace(dest_dir)
    if dest_ns is None:
        log.error(f"Shell.NameSpace returned None for {dest_dir}")
        return False

    dest_ns.CopyHere(item, COPY_FLAGS)

    # MTP copy is async — wait for completion
    for _ in range(timeout * 2):
        if item.IsFolder:
            if os.path.isdir(dest_path) and os.listdir(dest_path):
                return True
        else:
            if os.path.isfile(dest_path) and os.path.getsize(dest_path) > 0:
                return True
        time.sleep(0.5)

    log.warning(f"Timeout waiting for copy of {item.Name}")
    return False


def _copy_file_via_short_dir(shell, item, dest_path, timeout=120):
    """Copy a file whose final path exceeds MAX_PATH: CopyHere into a short
    temp directory (the Shell names the file itself, so only the directory
    prefix matters), then move it to dest_path with a \\\\?\\ long path.
    """
    import shutil
    import tempfile

    tmpdir = tempfile.mkdtemp(prefix='mtp_')
    try:
        tmp_path = os.path.join(tmpdir, item.Name)
        tmp_ns = shell.NameSpace(tmpdir)
        if tmp_ns is None:
            log.error(f"Shell.NameSpace returned None for {tmpdir}")
            return False

        tmp_ns.CopyHere(item, COPY_FLAGS)

        # Wait for the copy to complete: size > 0 and stable across polls.
        last_size = -1
        for _ in range(timeout * 2):
            if os.path.isfile(tmp_path):
                size = os.path.getsize(tmp_path)
                if size > 0 and size == last_size:
                    break
                last_size = size
            time.sleep(0.5)
        else:
            log.warning(f"Timeout waiting for copy of {item.Name}")
            return False

        long_dest = '\\\\?\\' + os.path.abspath(dest_path)
        shutil.move(tmp_path, long_dest)
        return True
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def sync_from_device(staging_dir, name_filter=None, include_standalone_notebooks=False,
                     include_screenshots=False, progress_callback=None):
    """Sync annotation data from connected Kindle Scribe to local staging directory.

    Copies:
    - .sdr folders (contain .yjr annotation files)
    - .kfx files (for PID mapping)
    - PDOC notebooks (annotation overlay data for the matched documents)
    - Optionally: standalone UUID notebooks (+ their thumbnails)
    - Optionally: screenshots

    Args:
        staging_dir: Local directory to stage files into (mirrors device structure)
        name_filter: Only sync documents whose name contains this string
        include_standalone_notebooks: Also sync standalone (UUID) notebooks
        include_screenshots: Also sync device screenshots
        progress_callback: Called with (message, current, total)

    Returns dict with keys: yjr_dir, notebooks_dir, synced_docs, synced_notebooks,
    synced_screenshots
    """
    shell = _get_shell()

    # Find device
    scribe, device_name = find_kindle_scribe()
    if not scribe:
        raise RuntimeError(
            "No Kindle Scribe found. Is it connected via USB and in "
            "file-transfer mode (i.e. you haven't tapped Disconnect on the device)?")
    log.info(f"Found device: {device_name}")

    # Navigate to Internal Storage
    internal = _find_child(scribe.GetFolder, 'Internal Storage')
    if not internal:
        raise RuntimeError("Cannot access Internal Storage on the device")

    notebooks_folder = _find_child(internal.GetFolder, '.notebooks')
    documents_folder = _find_child(internal.GetFolder, 'documents')

    if not notebooks_folder:
        raise RuntimeError("No .notebooks/ found on device")
    if not documents_folder:
        raise RuntimeError("No documents/ found on device")

    staging = Path(staging_dir)
    docs_staging = staging / 'documents'
    nb_staging = staging / '.notebooks'

    # Phase 1: walk the whole documents/ tree for .sdr folders (+ sibling
    # .kfx files). Send-to-Kindle content lives in Downloads/Items*, but
    # users also keep USB-copied documents elsewhere under documents/.
    # The personal-document ID pattern filters out everything else
    # (store books, sideloaded ebook sidecars, legacy .pds/.pdt PDFs).
    skip_dirs = {'dictionaries', '.cache', '.kindle'}

    def _doc_folders(folder, rel='', depth=0):
        yield folder, rel
        if depth >= 3:
            return
        for child in folder.GetFolder.Items():
            if (child.IsFolder and not child.Name.endswith('.sdr')
                    and child.Name not in skip_dirs):
                child_rel = f"{rel}/{child.Name}" if rel else child.Name
                yield from _doc_folders(child, child_rel, depth + 1)

    doc_items = []
    kfx_by_asin = {}
    for folder, rel in _doc_folders(documents_folder):
        for item in folder.GetFolder.Items():
            asin_m = ASIN_RE.search(item.Name)
            if not asin_m:
                continue
            asin = asin_m.group(1)

            if item.IsFolder and item.Name.endswith('.sdr'):
                # Check if it has a .yjr file
                has_yjr = any(c.Name.endswith('.yjr') for c in item.GetFolder.Items())
                if not has_yjr:
                    continue

                doc_name = re.sub(r'\.pdf_[A-F0-9]{32}\.sdr$', '', item.Name)
                doc_name = re.sub(r'_[A-F0-9]{32}\.sdr$', '', doc_name)

                if name_filter and name_filter.lower() not in doc_name.lower():
                    continue

                doc_items.append({
                    'sdr_item': item,
                    'asin': asin,
                    'doc_name': doc_name,
                    'rel': rel,
                })
            elif not item.IsFolder and item.Name.endswith('.kfx'):
                kfx_by_asin[(asin, rel)] = item

    log.info(f"Found {len(doc_items)} annotated document(s) on device")

    synced_asins = set()
    for i, doc in enumerate(doc_items):
        if progress_callback:
            progress_callback(f"Copying {doc['doc_name']}", i, len(doc_items))

        dest = str(docs_staging / doc['rel']) if doc['rel'] else str(docs_staging)
        log.info(f"  [{i+1}/{len(doc_items)}] {doc['doc_name']}")

        # Copy only the .yjr file(s) out of the .sdr folder. Some .sdr
        # folders contain large assets/data subtrees that are not needed
        # and can hang whole-folder MTP copies indefinitely.
        sdr_dest = os.path.join(dest, doc['sdr_item'].Name)
        yjr_items = [c for c in doc['sdr_item'].GetFolder.Items()
                     if c.Name.endswith('.yjr')]
        ok = all(_copy_item_to(shell, yjr, sdr_dest) for yjr in yjr_items)
        if ok and yjr_items:
            synced_asins.add(doc['asin'])
        else:
            log.warning(f"    Failed to copy .yjr for {doc['doc_name']}")
            continue

        # Copy .kfx file
        kfx = kfx_by_asin.get((doc['asin'], doc['rel']))
        if kfx:
            _copy_item_to(shell, kfx, dest)

    # Phase 2: Copy PDOC notebooks for synced documents
    pdoc_copied = 0
    for item in notebooks_folder.GetFolder.Items():
        if not item.IsFolder or '!!PDOC!!' not in item.Name:
            continue
        nb_asin = item.Name.split('!!')[0]
        if nb_asin in synced_asins:
            if progress_callback:
                progress_callback(f"Copying notebook for {nb_asin[:16]}...",
                                  pdoc_copied, len(synced_asins))
            log.info(f"  Copying PDOC notebook: {item.Name}")
            _copy_item_to(shell, item, str(nb_staging))
            pdoc_copied += 1

    # Phase 3: Optionally copy standalone notebooks (+ their thumbnails)
    standalone_copied = 0
    if include_standalone_notebooks:
        standalone = [
            item for item in notebooks_folder.GetFolder.Items()
            if item.IsFolder and UUID_RE.match(item.Name)
        ]
        if name_filter:
            log.info(f"Note: --filter doesn't apply to standalone notebooks (they have UUID names)")
        log.info(f"Syncing {len(standalone)} standalone notebook(s)")
        for i, item in enumerate(standalone):
            if progress_callback:
                progress_callback(f"Copying notebook {item.Name[:16]}...",
                                  i, len(standalone))
            _copy_item_to(shell, item, str(nb_staging))
            standalone_copied += 1

        thumbs = _find_child(notebooks_folder.GetFolder, 'thumbnails')
        if thumbs:
            if progress_callback:
                progress_callback("Copying notebook thumbnails...", 0, 0)
            thumb_staging = str(nb_staging / 'thumbnails')
            copied = sum(
                1 for t in thumbs.GetFolder.Items()
                if not t.IsFolder and t.Name.lower().endswith('.png')
                and _copy_item_to(shell, t, thumb_staging)
            )
            log.info(f"Synced {copied} notebook thumbnail(s)")

    # Phase 4: Optionally copy screenshots
    screenshots_copied = 0
    if include_screenshots:
        shots = _find_child(internal.GetFolder, 'screenshots')
        if shots:
            if progress_callback:
                progress_callback("Copying screenshots...", 0, 0)
            ss_staging = str(staging / 'screenshots')
            for item in shots.GetFolder.Items():
                if item.IsFolder:
                    continue
                if not item.Name.lower().endswith(('.png', '.jpg', '.jpeg')):
                    continue
                if _copy_item_to(shell, item, ss_staging):
                    screenshots_copied += 1
            log.info(f"Synced {screenshots_copied} screenshot(s)")

    if progress_callback:
        progress_callback("Sync complete", 1, 1)

    # Return the staging directories in the same format resolve_scribe_dir uses
    yjr_dir = str(docs_staging)
    notebooks_dir = str(nb_staging)

    log.info(f"Sync complete: {len(synced_asins)} docs, {pdoc_copied} PDOC notebooks, "
             f"{standalone_copied} standalone notebooks")
    log.info(f"Staged to: {staging_dir}")

    return {
        'yjr_dir': yjr_dir,
        'notebooks_dir': notebooks_dir,
        'synced_docs': len(synced_asins),
        'synced_notebooks': standalone_copied,
        'synced_screenshots': screenshots_copied,
    }


if __name__ == '__main__':
    # Subprocess entry point used by server.py: Shell COM must not run on a
    # background thread of the Flask process (native crashes take the whole
    # server down), so the web UI runs the sync as an isolated process and
    # reads JSON progress lines from stdout.
    import argparse
    import json
    import sys

    parser = argparse.ArgumentParser(description='Sync from a Kindle Scribe')
    parser.add_argument('--staging')
    parser.add_argument('--filter', default=None)
    parser.add_argument('--include-notebooks', action='store_true')
    parser.add_argument('--include-screenshots', action='store_true')
    parser.add_argument('--json-progress', action='store_true',
                        help='Emit JSON progress lines on stdout')
    parser.add_argument('--detect', action='store_true',
                        help='Just report the connected device as JSON and exit')
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, stream=sys.stderr,
                        format='%(levelname)s: %(message)s')

    if args.detect:
        try:
            _, device_name = find_kindle_scribe()
        except Exception:
            device_name = None
        print(json.dumps({'device': device_name}), flush=True)
        sys.exit(0)

    if not args.staging:
        parser.error('--staging is required unless --detect is given')

    callback = None
    if args.json_progress:
        def callback(message, current, total):
            print(json.dumps({'progress': {
                'message': message, 'current': current, 'total': total}}),
                flush=True)

    try:
        result = sync_from_device(
            args.staging,
            name_filter=args.filter,
            include_standalone_notebooks=args.include_notebooks,
            include_screenshots=args.include_screenshots,
            progress_callback=callback,
        )
    except Exception as e:
        print(json.dumps({'error': str(e)}), flush=True)
        sys.exit(1)
    print(json.dumps({'result': result}), flush=True)
