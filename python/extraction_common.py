"""Shared helpers for extracting receipts with Azure Document Intelligence.

Downloads receipt files from Blob Storage, shrinks them to fit the
Document Intelligence size limit, analyses them, and caches each result as
JSON in a separate extraction container.
"""
import io
import json
import os
from pathlib import Path
from urllib.parse import unquote, urlparse

import pypdfium2 as pdfium
from azure.ai.documentintelligence import DocumentIntelligenceClient
from azure.core.credentials import AzureKeyCredential
from azure.core.exceptions import ResourceNotFoundError
from azure.storage.blob import BlobClient, ContentSettings
from dotenv import load_dotenv
from PIL import Image, ImageOps

REPO_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(REPO_ROOT / ".env")


DI_ENDPOINT = os.environ["AZURE_DOCINTEL_ENDPOINT"]
DI_KEY = os.environ["AZURE_DOCINTEL_KEY"]
BLOB_CONNECTION = os.environ["AZURE_STORAGE_CONNECTION_STRING"]
CONTAINER = os.environ["AZURE_STORAGE_CONTAINER"]
EXTRACTION_CONTAINER = os.environ["AZURE_EXTRACTION_CONTAINER"]


MODELS = {"receipt": "prebuilt-receipt", "invoice": "prebuilt-invoice"}
DI_LIMIT = 4 * 1024 * 1024          # Free tier maximum file size (4 MB)

KEY_FIELDS = {
    "receipt": ("Total", "TransactionDate", "MerchantName"),
    "invoice": ("InvoiceTotal", "InvoiceDate", "VendorName"),
}
OTHER_MODEL = {"receipt": "invoice", "invoice": "receipt"}

PDF_PAGES = 2                 # the free tier analyses only the first two pages
PDF_DPIS = (200, 150, 100)    # tried in order until the copy fits
PDF_JPEG_QUALITY = 80

def key_field_count(result, model):
    """How many of the model's key fields have a parsed value."""
    docs = result.get("documents") or []
    if not docs:
        return 0
    fields = docs[0].get("fields") or {}
    count = 0
    for name in KEY_FIELDS[model]:
        field = fields.get(name) or {}
        # a parsed value is stored under a key starting with "value"
        if any(k.startswith("value") and v is not None for k, v in field.items()):
            count += 1
    return count

class PermanentError(Exception):
    """Raised for failures that will not succeed on retry."""


def blob_name_from_url(blob_url):
    """Return the blob name from a URL of the form
    https://<account>.blob.core.windows.net/<container>/<name>.
    """
    path = unquote(urlparse(blob_url).path)
    return path.split("/", 2)[2]


def download(blob_url):
    """Download and return the contents of the receipt blob at blob_url."""
    blob = BlobClient.from_connection_string(
        BLOB_CONNECTION, CONTAINER, blob_name_from_url(blob_url))
    return blob.download_blob().readall()

def shrink_pdf(data, limit):
    """A smaller copy of a PDF, for analysis only: its first PDF_PAGES pages
    rendered as JPEG images and wrapped back into a PDF. The original blob
    is never changed. Returns None if it can't be made to fit."""
    pdf = pdfium.PdfDocument(data)
    try:
        pages = min(len(pdf), PDF_PAGES)
        for dpi in PDF_DPIS:
            images = [pdf[i].render(scale=dpi / 72).to_pil().convert("RGB")
                      for i in range(pages)]
            buffer = io.BytesIO()
            images[0].save(buffer, format="PDF", save_all=True,
                           append_images=images[1:], resolution=dpi,
                           quality=PDF_JPEG_QUALITY)
            if buffer.tell() <= limit:
                return buffer.getvalue()
    finally:
        pdf.close()
    return None

def shrink_image(data):
    """A smaller JPEG copy of an image: EXIF orientation applied, then
    downscaled until it fits DI_LIMIT. Returns None if it can't be made to fit."""
    img = Image.open(io.BytesIO(data))
    img = ImageOps.exif_transpose(img)
    img = img.convert("RGB")

    max_side = 3000
    while max_side >= 500:
        small = img.copy()
        small.thumbnail((max_side, max_side))
        buf = io.BytesIO()
        small.save(buf, format="JPEG", quality=85)
        if buf.tell() <= DI_LIMIT:
            return buf.getvalue()
        max_side = int(max_side * 0.8)
    return None


def shrink_if_needed(data, content_type):
    """Return a version of the file that fits within DI_LIMIT.

    The original if it fits; otherwise an in-memory copy made the right way
    for the file type, so a PDF is never opened as an image:
      - PDF:   first PDF_PAGES pages rendered as JPEG images (shrink_pdf)
      - image: downscaled and re-encoded as JPEG (shrink_image)
    The original blob in storage is never modified.

    Raises:
        PermanentError: If the file can't be made to fit.
    """
    if len(data) <= DI_LIMIT:
        return data

    if content_type == "application/pdf":
        smaller = shrink_pdf(data, DI_LIMIT)
    else:
        smaller = shrink_image(data)

    if smaller is None:
        raise PermanentError(f"{content_type} too large even after shrinking "
                             f"({len(data):,} > {DI_LIMIT:,} bytes)")
    print(f"Shrunk {content_type} {len(data):,} -> {len(smaller):,} bytes")
    return smaller

def analyse(data, model):
    """Analyse the file with Document Intelligence and return the result."""
    client = DocumentIntelligenceClient(DI_ENDPOINT, AzureKeyCredential(DI_KEY))
    poller = client.begin_analyze_document(
        MODELS[model],
        body=io.BytesIO(data),
        content_type="application/octet-stream",
        locale="en-AU",
    )
    return poller.result()


# Extraction cache: Document Intelligence results stored as JSON in their
# own container.

def extraction_blob_name(receipt_blob_url, model):
    """Return the cache blob name for a receipt and model.

    The name is the receipt's blob name followed by the model name, for
    example <receipt blob name>.prebuilt-receipt.json.
    """
    return f"{blob_name_from_url(receipt_blob_url)}.{MODELS[model]}.json"


def _extraction_blob(name):
    """Return a client for a blob in the extraction container."""
    return BlobClient.from_connection_string(
        BLOB_CONNECTION, EXTRACTION_CONTAINER, name)


def load_extraction(name):
    """Return the cached result as a dict, or None if it is not cached."""
    try:
        return json.loads(_extraction_blob(name).download_blob().readall())
    except ResourceNotFoundError:
        return None


def save_extraction(name, result_dict):
    """Save a result dict to the cache as JSON, replacing any existing copy."""
    _extraction_blob(name).upload_blob(
        json.dumps(result_dict, default=str).encode("utf-8"),
        overwrite=True,
        content_settings=ContentSettings(content_type="application/json"),
    )


def extract(receipt_blob_url, content_type, model):
    """Return (result_dict, from_cache) for one receipt.

    A cached result is returned without calling Document Intelligence.
    Otherwise the file is downloaded, shrunk if needed, analysed and saved
    to the cache. The cache is written before the caller updates the
    database, so if that update fails the next run reuses the result.

    Raises:
        PermanentError: If the file cannot be made small enough to analyse.
    """
    name = extraction_blob_name(receipt_blob_url, model)
    cached = load_extraction(name)
    if cached is not None:
        return cached, True

    data = shrink_if_needed(download(receipt_blob_url), content_type)
    result_dict = analyse(data, model).as_dict()
    save_extraction(name, result_dict)
    return result_dict, False