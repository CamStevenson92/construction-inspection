"""
Generates a Word (.docx) site inspection report from a list of PhotoData objects.

Template placeholders (place these in your Word template):
    <<SITE_NAME>>         Project / site name
    <<PROJECT_NUMBER>>    Project number / reference
    <<INSPECTOR_NAME>>    Inspector's name
    <<INSPECTION_DATE>>   Date of earliest photo (or today)
    <<REPORT_DATE>>       Date the report was generated
    <<SITE_ADDRESS>>      Site address (filled by user)
    <<TOTAL_PHOTOS>>      Total number of photos included

Photos are inserted after all template content, one per page section,
formatted as a two-column table: [Photo] | [Metadata & Notes].

If no template is provided, a clean default document is generated.
"""

import os
import io
import re
from datetime import date
from typing import List, Optional

from docx import Document
from docx.shared import Inches, Pt, RGBColor, Cm
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.enum.table import WD_ALIGN_VERTICAL
from docx.oxml.ns import qn
from docx.oxml import OxmlElement
from PIL import Image as PILImage

from models import PhotoData


PLACEHOLDER_RE = re.compile(r"<<([A-Z_]+)>>")

MAX_PHOTO_WIDTH_INCHES = 3.5   # max width in the table cell
MAX_PHOTO_HEIGHT_INCHES = 3.0


def generate_report(
    photos: List[PhotoData],
    output_path: str,
    template_path: Optional[str] = None,
    site_name: str = "",
    project_number: str = "",
    inspector_name: str = "",
    site_address: str = "",
) -> str:
    """
    Build the report and save to output_path.
    Returns the final output_path.
    """
    if template_path and os.path.isfile(template_path):
        doc = Document(template_path)
    else:
        doc = _default_document()

    inspection_date = _earliest_date(photos) or date.today()
    report_date = date.today()

    replacements = {
        "SITE_NAME": site_name or "—",
        "PROJECT_NUMBER": project_number or "—",
        "INSPECTOR_NAME": inspector_name or "—",
        "INSPECTION_DATE": inspection_date.strftime("%d %B %Y"),
        "REPORT_DATE": report_date.strftime("%d %B %Y"),
        "SITE_ADDRESS": site_address or "—",
        "TOTAL_PHOTOS": str(len(photos)),
    }

    # Replace placeholders in all paragraphs (including headers/footers)
    _replace_placeholders(doc, replacements)

    # Add photo entries
    _add_photo_section_heading(doc)
    for i, photo in enumerate(photos, 1):
        _add_photo_entry(doc, photo, i)
        if i < len(photos):
            _add_page_break(doc)

    doc.save(output_path)
    return output_path


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _default_document() -> Document:
    doc = Document()

    # Title
    title = doc.add_heading("Site Inspection Report", 0)
    title.alignment = WD_ALIGN_PARAGRAPH.CENTER

    doc.add_paragraph()  # spacer

    # Header info table
    info = doc.add_table(rows=6, cols=2)
    info.style = "Table Grid"
    labels = [
        ("Site / Project:", "<<SITE_NAME>>"),
        ("Project Number:", "<<PROJECT_NUMBER>>"),
        ("Site Address:", "<<SITE_ADDRESS>>"),
        ("Inspector:", "<<INSPECTOR_NAME>>"),
        ("Inspection Date:", "<<INSPECTION_DATE>>"),
        ("Report Generated:", "<<REPORT_DATE>>"),
    ]
    for row, (label, val) in zip(info.rows, labels):
        row.cells[0].text = label
        row.cells[1].text = val
        row.cells[0].paragraphs[0].runs[0].bold = True

    doc.add_paragraph()
    doc.add_paragraph(f"Total Photos: <<TOTAL_PHOTOS>>")
    return doc


def _replace_placeholders(doc: Document, replacements: dict) -> None:
    def _replace_in_para(para):
        # Rebuild run text so placeholders split across runs are handled
        full_text = "".join(r.text for r in para.runs)
        new_text = PLACEHOLDER_RE.sub(lambda m: replacements.get(m.group(1), m.group(0)), full_text)
        if new_text != full_text:
            for run in para.runs:
                run.text = ""
            if para.runs:
                para.runs[0].text = new_text
            else:
                para.add_run(new_text)

    for para in doc.paragraphs:
        _replace_in_para(para)

    for table in doc.tables:
        for row in table.rows:
            for cell in row.cells:
                for para in cell.paragraphs:
                    _replace_in_para(para)

    for section in doc.sections:
        for hdr in (section.header, section.footer):
            if hdr:
                for para in hdr.paragraphs:
                    _replace_in_para(para)


def _add_photo_section_heading(doc: Document) -> None:
    doc.add_page_break()
    h = doc.add_heading("Photographic Record", level=1)
    h.alignment = WD_ALIGN_PARAGRAPH.LEFT


def _add_photo_entry(doc: Document, photo: PhotoData, index: int) -> None:
    # Photo number heading
    heading = doc.add_heading(f"Photo {index}  —  {photo.filename}", level=2)

    # Build a 2-column table: left = photo, right = metadata + notes
    table = doc.add_table(rows=1, cols=2)
    table.style = "Table Grid"

    col_widths = [Inches(3.6), Inches(3.6)]
    for i, cell in enumerate(table.rows[0].cells):
        cell.width = col_widths[i]
        _set_cell_valign(cell, "top")

    left_cell = table.rows[0].cells[0]
    right_cell = table.rows[0].cells[1]

    # Insert photo image
    _insert_photo_image(left_cell, photo)

    # Right cell: metadata
    _add_metadata(right_cell, photo, index)


def _insert_photo_image(cell, photo: PhotoData) -> None:
    try:
        img = PILImage.open(photo.file_path)
        # Respect EXIF orientation
        from PIL import ImageOps
        img = ImageOps.exif_transpose(img)

        # Scale to fit within max dimensions
        w, h = img.size
        max_w = int(MAX_PHOTO_WIDTH_INCHES * 96)
        max_h = int(MAX_PHOTO_HEIGHT_INCHES * 96)
        ratio = min(max_w / w, max_h / h, 1.0)
        new_w = int(w * ratio)
        new_h = int(h * ratio)
        img = img.resize((new_w, new_h), PILImage.LANCZOS)

        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=85)
        buf.seek(0)

        para = cell.paragraphs[0]
        run = para.add_run()
        run.add_picture(buf, width=Inches(MAX_PHOTO_WIDTH_INCHES))

        # Duplicate / similar indicator below the image
        if photo.is_duplicate:
            p = cell.add_paragraph(f"⚠ DUPLICATE of {photo.similar_to}")
            p.runs[0].font.color.rgb = RGBColor(0xCC, 0x00, 0x00)
            p.runs[0].font.bold = True
        elif photo.similar_to:
            p = cell.add_paragraph(f"Similar to {photo.similar_to}")
            p.runs[0].font.color.rgb = RGBColor(0xFF, 0x88, 0x00)

    except Exception as e:
        cell.paragraphs[0].text = f"[Image unavailable: {e}]"


def _add_metadata(cell, photo: PhotoData, index: int) -> None:
    def _row(label: str, value: str, bold_val: bool = False):
        p = cell.add_paragraph()
        run_label = p.add_run(f"{label}: ")
        run_label.bold = True
        run_val = p.add_run(value or "—")
        if bold_val:
            run_val.bold = True
        p.paragraph_format.space_after = Pt(2)

    _row("Date / Time", photo.datetime_label)
    _row("Coordinates", photo.coords_label)
    if photo.altitude_label:
        _row("Altitude", photo.altitude_label)
    _row("Direction", photo.direction_label)
    if photo.make or photo.model:
        _row("Camera", f"{photo.make} {photo.model}".strip())

    if photo.weather:
        _row("Weather", photo.weather.summary())

    # Notes
    cell.add_paragraph()  # spacer

    def _notes_section(title: str, text: str, color: RGBColor):
        h = cell.add_paragraph()
        run = h.add_run(title)
        run.bold = True
        run.font.color.rgb = color
        body = cell.add_paragraph(text.strip() if text.strip() else "(none noted)")
        body.paragraph_format.space_after = Pt(4)

    _notes_section("What Was Inspected:", photo.what_inspected, RGBColor(0x00, 0x4F, 0x9F))
    _notes_section("Issues Found:", photo.issues_found, RGBColor(0xCC, 0x44, 0x00))
    _notes_section("Actions Required:", photo.actions_required, RGBColor(0x33, 0x66, 0x00))


def _add_page_break(doc: Document) -> None:
    doc.add_page_break()


def _set_cell_valign(cell, align: str = "top") -> None:
    tc = cell._tc
    tcPr = tc.get_or_add_tcPr()
    vAlign = OxmlElement("w:vAlign")
    vAlign.set(qn("w:val"), align)
    tcPr.append(vAlign)


def _earliest_date(photos: List[PhotoData]) -> Optional[date]:
    dates = [p.datetime_taken.date() for p in photos if p.datetime_taken]
    return min(dates) if dates else None
