"""Findet den Swiss QR Code in einem PDF oder Bild."""
from __future__ import annotations

import io
import threading

import pypdfium2 as pdfium
import zxingcpp
from PIL import Image

# pdfium ist nicht thread-sicher: alle Zugriffe (Scan, Vorschau) laufen über diese Sperre
PDFIUM_LOCK = threading.RLock()

# Auflösungen pro Seite, aufsteigend: meist reicht die erste, höhere nur bei schlechten Scans
DPI_STEPS = (150, 250, 400)


def _scan_image(img: Image.Image) -> list[str]:
    found = zxingcpp.read_barcodes(img, formats=zxingcpp.BarcodeFormat.QRCode)
    return [b.text for b in found if b.text.lstrip().startswith("SPC")]


def find_swiss_qr(data: bytes, mime: str = "") -> list[str]:
    """Liefert die Swiss-QR-Payloads der ersten Seite (von hinten), auf der einer gefunden wird.

    Der Zahlteil steht fast immer auf der letzten Seite. Deshalb wird von hinten gesucht und
    abgebrochen, sobald eine Seite einen Swiss QR Code enthält.
    """
    if "pdf" in mime or data[:5] == b"%PDF-":
        with PDFIUM_LOCK:
            pdf = pdfium.PdfDocument(data)
            try:
                for i in reversed(range(len(pdf))):
                    page = pdf[i]
                    for dpi in DPI_STEPS:
                        hits = _scan_image(page.render(scale=dpi / 72).to_pil())
                        if hits:
                            return list(dict.fromkeys(hits))
                return []
            finally:
                pdf.close()
    img = Image.open(io.BytesIO(data))
    return list(dict.fromkeys(_scan_image(img.convert("RGB"))))
