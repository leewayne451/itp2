"""Generate text and image-only synthetic PDFs for the privacy workflow demo."""
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont
from reportlab.lib.colors import HexColor
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas


ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "output" / "pdf" / "pdpa-synthetic-test-document.pdf"
IMAGE_OUTPUT = ROOT / "output" / "pdf" / "pdpa-image-only-test-document.pdf"


def build():
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    styles = getSampleStyleSheet()
    title = ParagraphStyle("SyntheticTitle", parent=styles["Title"], fontName="Helvetica-Bold",
                           fontSize=19, leading=23, textColor=HexColor("#173B57"),
                           alignment=TA_CENTER, spaceAfter=5 * mm)
    subtitle = ParagraphStyle("SyntheticSubtitle", parent=styles["BodyText"], fontSize=10,
                              leading=14, textColor=HexColor("#52606D"), alignment=TA_CENTER,
                              spaceAfter=8 * mm)
    body = ParagraphStyle("SyntheticBody", parent=styles["BodyText"], fontSize=10.5,
                          leading=15, textColor=HexColor("#172B3A"))
    note = ParagraphStyle("SyntheticNote", parent=body, fontSize=9.5, leading=14,
                          textColor=HexColor("#52606D"), spaceBefore=7 * mm)

    doc = SimpleDocTemplate(str(OUTPUT), pagesize=A4, rightMargin=22 * mm, leftMargin=22 * mm,
                            topMargin=20 * mm, bottomMargin=20 * mm,
                            title="Synthetic Personal Data Test Document",
                            author="ServiceJourney POC")
    story = [
        Paragraph("Synthetic Personal Data Test Document", title),
        Paragraph("Safe fixture for testing automated detection, irreversible PDF redaction and retention", subtitle),
        Paragraph("This document contains invented test values. It must not be used as proof of identity or for any real transaction.", body),
        Spacer(1, 7 * mm),
    ]
    rows = [
        ["Field", "Synthetic value", "Expected detector"],
        ["Name", "Synthetic Test Person", "Not masked in iteration 1"],
        ["NRIC / FIN", "S1234567D", "nric_fin"],
        ["Mobile phone", "+65 9123 4567", "phone"],
        ["Email", "glenn.test@example.gov.sg", "email"],
        ["Residential address", "123 Example Road, Singapore 123456", "address"],
        ["Request reference", "REQ-DEMO-2026-001", "Must remain visible"],
    ]
    table = Table(rows, colWidths=[42 * mm, 75 * mm, 45 * mm], repeatRows=1)
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), HexColor("#173B57")),
        ("TEXTCOLOR", (0, 0), (-1, 0), HexColor("#FFFFFF")),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTNAME", (0, 1), (-1, -1), "Helvetica"),
        ("FONTSIZE", (0, 0), (-1, -1), 9.5),
        ("LEADING", (0, 0), (-1, -1), 13),
        ("BACKGROUND", (0, 1), (-1, -1), HexColor("#F7FAFC")),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [HexColor("#FFFFFF"), HexColor("#F3F7FA")]),
        ("GRID", (0, 0), (-1, -1), 0.5, HexColor("#D9D9D9")),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 7),
        ("RIGHTPADDING", (0, 0), (-1, -1), 7),
        ("TOPPADDING", (0, 0), (-1, -1), 7),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
    ]))
    story += [table,
              Paragraph("Expected result: the protected PDF replaces the NRIC, phone, email and address text with black redaction areas. The request reference remains searchable and visible.", note)]
    doc.build(story)
    print(OUTPUT)

    # A scanned/image-only fixture: the PDF page contains one raster image and no
    # extractable text. It must enter the manual privacy-review queue.
    image = Image.new("RGB", (1240, 1754), "white")
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default(size=28)
    bold = ImageFont.load_default(size=40)
    lines = [
        ("Synthetic Scanned Document", bold),
        ("Invented test values only - not proof of identity", font),
        ("", font),
        ("NRIC / FIN: S1234567D", font),
        ("Mobile: +65 9123 4567", font),
        ("Email: glenn.test@example.gov.sg", font),
        ("Address: 123 Example Road, Singapore 123456", font),
        ("Reference: REQ-IMAGE-DEMO-001", font),
    ]
    y = 150
    for line, line_font in lines:
        draw.text((120, y), line, fill="#173B57", font=line_font)
        y += 80
    png = BytesIO()
    image.save(png, format="PNG")
    png.seek(0)
    pdf = canvas.Canvas(str(IMAGE_OUTPUT), pagesize=A4)
    width, height = A4
    pdf.drawImage(ImageReader(png), 0, 0, width=width, height=height)
    pdf.showPage()
    pdf.save()
    print(IMAGE_OUTPUT)


if __name__ == "__main__":
    build()
