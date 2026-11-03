"""Download receipts by id and open them in Preview.

Usage:
    python3 python/open_receipts.py 39 10            -> local database
    python3 python/open_receipts.py 39 10 azure      -> Azure database

Files are saved to data/review/ (gitignored) as <receipt id>.<extension>,
the extension chosen from the content type. A file whose contents aren't
of its content type is neither saved nor opened.
"""
import subprocess
import sys

from engine_setup import engine_setup
from extraction_common import REPO_ROOT, download
from mail_common import review_file_name
from sqlalchemy import text

OUT_DIR = REPO_ROOT / "data" / "review"


def open_receipt(receipt_id, blob_url, content_type):
    """Download a receipt into OUT_DIR and open it in Preview. Returns the
    path, or None (with a warning printed) if the file was refused."""
    data = download(blob_url)
    try:
        name = review_file_name(receipt_id, content_type, data)
    except ValueError as e:
        print(f"  WARNING: receipt {receipt_id} not opened: {e}")
        return None
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUT_DIR / name
    path.write_bytes(data)
    subprocess.run(["open", "-a", "Preview", str(path)])
    return path


def main():
    args = sys.argv[1:]
    target = "azure" if "azure" in args else "local"
    ids = [int(a) for a in args if a.isdigit()]
    if not ids:
        sys.exit("usage: open_receipts.py <receipt_id> [<receipt_id> ...] [azure]")

    engine = engine_setup(target)
    try:
        with engine.connect() as conn:
            for rid in ids:
                row = conn.execute(
                    text("SELECT blob_url, content_type FROM RECEIPT "
                         "WHERE receipt_id = :id"),
                    {"id": rid},
                ).fetchone()
                if row is None:
                    print(f"{rid}: no such receipt")
                    continue
                path = open_receipt(rid, row.blob_url, row.content_type)
                if path:
                    print(f"{rid}: opened {path.name}")
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()