"""What counts as a file worth keeping from an email: one definition, so
every script that reads the mailbox agrees on it."""

# Accepted content types, and the extension a file of each is saved with
EXTENSIONS = {"application/pdf": ".pdf", "image/jpeg": ".jpg", "image/png": ".png",
              "image/heic": ".heic", "image/tiff": ".tiff"}
ACCEPT = set(EXTENSIONS)
MIN_BYTES = 5_000


def has_signature(data, content_type):
    """True if data starts with the magic bytes of content_type's format."""
    if content_type == "application/pdf":
        return data.startswith(b"%PDF")
    if content_type == "image/jpeg":
        return data.startswith(b"\xff\xd8\xff")
    if content_type == "image/png":
        return data.startswith(b"\x89PNG")
    if content_type == "image/tiff":
        return data.startswith((b"II*\x00", b"MM\x00*"))
    if content_type == "image/heic":
        return data[4:8] == b"ftyp"
    return False


def review_file_name(receipt_id, content_type, data):
    """The name to save a receipt under for opening: its id plus the
    extension for its content type. The sender's file name is never used:
    a name like receipt.terminal would run commands when opened.

    Raises:
        ValueError: If the content type isn't accepted, or the data isn't
            a file of that type.
    """
    if content_type not in EXTENSIONS:
        raise ValueError(f"content type {content_type!r} is not one that is opened")
    if not has_signature(data, content_type):
        raise ValueError(f"contents are not a {content_type} file; "
                         "consider marking it not an expense")
    return f"{receipt_id}{EXTENSIONS[content_type]}"


def receipt_files(msg):
    """Return [(filename, content_type, data), ...] in message order."""
    files = []
    for part in msg.walk():
        if part.get_content_maintype() == "multipart":
            continue
        if not part.get_filename():
            continue
        if part.get_content_type() not in ACCEPT:
            continue
        data = part.get_payload(decode=True)
        if not data or len(data) < MIN_BYTES:
            continue
        files.append((part.get_filename(), part.get_content_type(), data))
    return files