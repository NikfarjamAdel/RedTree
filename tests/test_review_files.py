"""Files saved for review: named from their content type, never the sender's
file name, and refused unless their contents are of that type."""
from email.message import EmailMessage

import open_receipts
import pytest
from mail_common import receipt_files, review_file_name

PDF = b"%PDF-1.7\n" + b"\x00" * 6000


def email_with(data, maintype, subtype, filename):
    msg = EmailMessage()
    msg["Subject"] = "Your receipt"
    msg.set_content("See attached.")
    msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=filename)
    return msg


def test_terminal_name_with_pdf_content_type_is_saved_as_pdf():
    # receipt.terminal would run commands when opened
    [(filename, content_type, data)] = receipt_files(
        email_with(PDF, "application", "pdf", "receipt.terminal"))
    assert filename == "receipt.terminal"
    assert review_file_name(39, content_type, data) == "39.pdf"


@pytest.mark.parametrize("content_type, data, name", [
    ("application/pdf", b"%PDF-1.4", "7.pdf"),
    ("image/jpeg", b"\xff\xd8\xff\xe0", "7.jpg"),
    ("image/png", b"\x89PNG\r\n\x1a\n", "7.png"),
    ("image/tiff", b"II*\x00", "7.tiff"),
    ("image/tiff", b"MM\x00*", "7.tiff"),
    ("image/heic", b"\x00\x00\x00\x18ftypheic", "7.heic"),
])
def test_each_type_gets_its_extension(content_type, data, name):
    assert review_file_name(7, content_type, data) == name


def test_contents_not_of_the_claimed_type_are_refused():
    script = b'<?xml version="1.0"?><plist><dict><key>CommandString</key>'
    with pytest.raises(ValueError, match="not an expense"):
        review_file_name(39, "application/pdf", script)


def test_type_not_accepted_is_refused():
    with pytest.raises(ValueError, match="not one that is opened"):
        review_file_name(39, "application/x-terminal", PDF)


# --- open_receipt: saves and opens only accepted files ---------------------
@pytest.fixture
def opened(monkeypatch, tmp_path):
    """Calls to `open`, with downloads and the review folder faked."""
    calls = []
    monkeypatch.setattr(open_receipts, "OUT_DIR", tmp_path)
    monkeypatch.setattr(open_receipts.subprocess, "run", calls.append)
    return calls


def test_accepted_file_is_saved_and_opened_in_preview(monkeypatch, tmp_path, opened):
    monkeypatch.setattr(open_receipts, "download", lambda url: PDF)
    path = open_receipts.open_receipt(39, "https://x/blob/receipt.terminal",
                                      "application/pdf")
    assert path == tmp_path / "39.pdf"
    assert path.read_bytes() == PDF
    assert opened == [["open", "-a", "Preview", str(path)]]


def test_mismatched_file_is_neither_saved_nor_opened(monkeypatch, tmp_path, opened, capsys):
    monkeypatch.setattr(open_receipts, "download", lambda url: b"#!/bin/sh\nrm -rf ~\n")
    assert open_receipts.open_receipt(39, "https://x/blob/a.pdf", "application/pdf") is None
    assert opened == []
    assert list(tmp_path.iterdir()) == []
    assert "WARNING: receipt 39 not opened" in capsys.readouterr().out
