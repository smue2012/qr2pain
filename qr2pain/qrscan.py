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


# Höchstens so viele Seiten nach dem ersten Fund weiter rückwärts prüfen (Ratenscheine auf mehreren Seiten)
MAX_EXTRA_PAGES = 24


def _scan_image(img: Image.Image) -> list[str]:
    """Swiss-QR-Payloads eines Bildes, von oben nach unten, links nach rechts."""
    found = zxingcpp.read_barcodes(img, formats=zxingcpp.BarcodeFormat.QRCode)
    hits = [b for b in found if b.text.lstrip().startswith("SPC")]
    hits.sort(key=lambda b: (b.position.top_left.y // 40, b.position.top_left.x))
    return [b.text for b in hits]


def _scan_page(pdf, i: int, dpis=DPI_STEPS) -> list[str]:
    page = pdf[i]
    for dpi in dpis:
        hits = _scan_image(page.render(scale=dpi / 72).to_pil())
        if hits:
            return hits
    return []


def find_swiss_qr(data: bytes, mime: str = "") -> list[str]:
    """Liefert alle Swiss-QR-Payloads eines Dokuments in Dokumentreihenfolge (ohne Doppelte).

    Der Zahlteil steht fast immer am Schluss. Deshalb wird von hinten gesucht. Nach dem ersten Fund
    werden die davorliegenden Seiten weiter geprüft, bis eine Seite ohne QR-Code kommt – so werden
    mehrere Einzahlungsscheine (z. B. Ratenscheine) erkannt, ohne lange Dokumente ganz zu scannen.
    """
    if "pdf" in mime or data[:5] == b"%PDF-":
        with PDFIUM_LOCK:
            pdf = pdfium.PdfDocument(data)
            try:
                pages: list[list[str]] = []
                found = False
                extra = 0
                for i in reversed(range(len(pdf))):
                    hits = _scan_page(pdf, i, DPI_STEPS if not found else DPI_STEPS[:2])
                    if hits:
                        found = True
                        pages.append(hits)
                    elif found:
                        break
                    if found:
                        extra += 1
                        if extra > MAX_EXTRA_PAGES:
                            break
                return list(dict.fromkeys(t for hits in reversed(pages) for t in hits))
            finally:
                pdf.close()
    img = Image.open(io.BytesIO(data))
    return list(dict.fromkeys(_scan_image(img.convert("RGB"))))
