"""
FILE: services/drive_sync.py
PURPOSE: Persist the SQLite databases across Streamlit Community Cloud restarts
         by backing them up to Google Drive — no paid host, no external DB.

HOW IT WORKS
  • restore_on_start()  — at app startup (ONCE per process), download the latest
        invoices.db + auth.db from a Drive folder into their local paths, BEFORE
        the app opens them. So a fresh container starts with your real data
        instead of an empty database.
  • request_backup()    — after a scan / send, snapshot the live databases and
        upload them to Drive. Debounced + run on a background thread, so the UI
        never waits. gzip-compressed to roughly halve the transfer.

SAFETY / SPEED
  • Snapshots use SQLite's online-backup API, so the copy is always consistent
    even if the DB is being written at that moment (no corruption).
  • Uploads are coalesced: many writes in a burst → one upload. At most one
    upload per DRIVE_SYNC_MIN_SECONDS.
  • Everything is wrapped so a Drive hiccup NEVER crashes the app — a failed
    backup just logs and moves on; the local DB keeps working.

AUTH
  Reuses the same Google account as Gmail. You must add the `drive.file` scope
  (see services/gmail_client.SCOPES) and regenerate the token once. `drive.file`
  only grants access to files THIS app creates — it cannot see the rest of your
  Drive.

ENV
  DRIVE_BACKUP            "1"/"0"  — enable/disable        (default "1")
  DRIVE_BACKUP_FOLDER     Drive folder name                (default "CFO-Copilot-Backup")
  DRIVE_SYNC_MIN_SECONDS  min seconds between uploads      (default "20")
  DB_PATH / AUTH_DB_PATH  local database paths (same ones the app already uses)
"""

import gzip
import io
import os
import sqlite3
import threading
import time
from pathlib import Path

_FOLDER_NAME  = os.getenv("DRIVE_BACKUP_FOLDER", "CFO-Copilot-Backup")
_MIN_INTERVAL = int(os.getenv("DRIVE_SYNC_MIN_SECONDS", "20"))
_ENABLED      = os.getenv("DRIVE_BACKUP", "1").strip().lower() not in ("0", "false", "no", "")

_lock            = threading.Lock()
_last_upload     = 0.0
_folder_id_cache = None
_restored_done   = False


def _db_targets() -> dict:
    """Map of {local_db_path: drive_filename}. Read lazily so env vars set by the
    startup secrets bridge are picked up."""
    return {
        os.getenv("DB_PATH", "invoices.db"):      "invoices.db.gz",
        os.getenv("AUTH_DB_PATH", "auth.db"):     "auth.db.gz",
    }


# ── Google Drive service (reuses the Gmail credentials) ────────────────────
def _drive():
    from googleapiclient.discovery import build
    from services.gmail_client import get_google_credentials
    creds = get_google_credentials()
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def _folder_id(svc) -> str:
    global _folder_id_cache
    if _folder_id_cache:
        return _folder_id_cache
    q = (f"name = '{_FOLDER_NAME}' and "
         "mimeType = 'application/vnd.google-apps.folder' and trashed = false")
    found = svc.files().list(q=q, spaces="drive", fields="files(id)").execute().get("files", [])
    if found:
        _folder_id_cache = found[0]["id"]
    else:
        meta = {"name": _FOLDER_NAME, "mimeType": "application/vnd.google-apps.folder"}
        _folder_id_cache = svc.files().create(body=meta, fields="id").execute()["id"]
    return _folder_id_cache


def _find_file(svc, folder_id: str, name: str):
    q = f"name = '{name}' and '{folder_id}' in parents and trashed = false"
    files = svc.files().list(q=q, spaces="drive", fields="files(id)").execute().get("files", [])
    return files[0]["id"] if files else None


# ── Snapshot (safe copy of a live SQLite db) → gzip bytes ──────────────────
def snapshot_gz(src_path: str) -> bytes | None:
    """Consistent snapshot of a live SQLite database, gzip-compressed.
    Uses the online-backup API so it's safe even mid-write. Returns None if the
    source doesn't exist yet."""
    if not Path(src_path).exists():
        return None
    tmp = f"{src_path}.snapshot"
    src = dst = None
    try:
        src = sqlite3.connect(src_path)
        dst = sqlite3.connect(tmp)
        with dst:
            src.backup(dst)            # atomic, consistent copy
        return gzip.compress(Path(tmp).read_bytes())
    finally:
        try:
            if src: src.close()
            if dst: dst.close()
        except Exception:
            pass
        try:
            os.remove(tmp)
        except Exception:
            pass


# ── Restore (download DBs from Drive at startup) ───────────────────────────
def restore_on_start() -> list:
    """Download the latest DB snapshots from Drive into their local paths.
    Runs at most ONCE per process (guarded), so reruns don't re-download or
    clobber in-session writes. Returns the list of restored local paths."""
    global _restored_done
    if not _ENABLED or _restored_done:
        return []
    _restored_done = True

    restored = []
    try:
        from googleapiclient.http import MediaIoBaseDownload
        svc = _drive()
        folder_id = _folder_id(svc)
        for local, remote in _db_targets().items():
            file_id = _find_file(svc, folder_id, remote)
            if not file_id:
                continue                       # first-ever run: nothing to restore
            buf = io.BytesIO()
            downloader = MediaIoBaseDownload(buf, svc.files().get_media(fileId=file_id))
            done = False
            while not done:
                _, done = downloader.next_chunk()
            data = gzip.decompress(buf.getvalue())
            Path(local).parent.mkdir(parents=True, exist_ok=True)
            with open(local, "wb") as f:
                f.write(data)
            restored.append(local)
        if restored:
            print(f"✅ Restored from Drive: {', '.join(restored)}")
    except Exception as e:
        # Never block startup on a backup problem — just run on local/empty DB.
        print(f"⚠️  Drive restore skipped ({e.__class__.__name__}): {e}")
    return restored


# ── Upload (push DB snapshots to Drive) ────────────────────────────────────
def _upload_all() -> None:
    try:
        from googleapiclient.http import MediaIoBaseUpload
        svc = _drive()
        folder_id = _folder_id(svc)
        for local, remote in _db_targets().items():
            gz = snapshot_gz(local)
            if gz is None:
                continue
            media = MediaIoBaseUpload(io.BytesIO(gz),
                                      mimetype="application/gzip", resumable=False)
            existing = _find_file(svc, folder_id, remote)
            if existing:
                svc.files().update(fileId=existing, media_body=media).execute()
            else:
                svc.files().create(body={"name": remote, "parents": [folder_id]},
                                   media_body=media, fields="id").execute()
        print("✅ Drive backup uploaded")
    except Exception as e:
        print(f"⚠️  Drive backup failed ({e.__class__.__name__}): {e}")


def request_backup(force: bool = False) -> None:
    """Trigger a backup. Safe to call after every scan/send: it debounces (at
    most one upload per DRIVE_SYNC_MIN_SECONDS) and runs on a background thread
    so the UI never waits. Pass force=True for critical moments (e.g. after
    'Send All') to bypass the debounce."""
    global _last_upload
    if not _ENABLED:
        return
    now = time.time()
    with _lock:
        if not force and (now - _last_upload) < _MIN_INTERVAL:
            return
        _last_upload = now
    threading.Thread(target=_upload_all, daemon=True).start()
