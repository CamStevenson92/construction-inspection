"""
Construction Site Inspection Photo Processor
Main PyQt6 application.

Workflow:
  1. Upload Photos  — select images, extract EXIF + weather in background
  2. Annotate       — scroll through photos, add inspection notes via keyboard/dictation
  3. Generate Report — pick template + output folder, produce Word document
"""

import os
import sys
import json
import logging
import shutil
from typing import List, Optional

from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QTabWidget, QPushButton, QFileDialog, QListWidget, QListWidgetItem,
    QLabel, QTextEdit, QSplitter, QProgressBar, QMessageBox,
    QLineEdit, QFormLayout, QGroupBox, QScrollArea, QFrame,
    QSizePolicy, QStatusBar, QToolBar, QStackedWidget
)
from PyQt6.QtCore import Qt, QThread, pyqtSignal, QSize, QRunnable, QThreadPool, QObject
from PyQt6.QtGui import QPixmap, QIcon, QFont, QImage, QColor, QPainter, QAction

from models import PhotoData
from processors import extract_photo_data, compute_phash, detect_duplicates, fetch_weather
from report_generator import generate_report

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
log = logging.getLogger(__name__)

SUPPORTED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".heic", ".heif", ".tiff", ".tif", ".webp"}
THUMB_SIZE = 120

# ---------------------------------------------------------------------------
# Worker signals / runnables (background processing)
# ---------------------------------------------------------------------------

class WorkerSignals(QObject):
    progress = pyqtSignal(int, str)      # (count_done, filename)
    photo_ready = pyqtSignal(object)     # PhotoData
    finished = pyqtSignal()
    error = pyqtSignal(str)


class PhotoProcessorRunnable(QRunnable):
    """Process a single photo: EXIF + phash + weather."""

    def __init__(self, filepath: str, signals: WorkerSignals):
        super().__init__()
        self.filepath = filepath
        self.signals = signals

    def run(self):
        try:
            photo = extract_photo_data(self.filepath)
            photo.phash = compute_phash(self.filepath)

            if photo.has_gps:
                photo.weather = fetch_weather(
                    photo.latitude, photo.longitude, photo.datetime_taken
                )

            self.signals.photo_ready.emit(photo)
        except Exception as e:
            log.exception("Failed processing %s", self.filepath)
            self.signals.error.emit(f"{os.path.basename(self.filepath)}: {e}")


class BatchProcessorThread(QThread):
    progress = pyqtSignal(int, int, str)   # (done, total, filename)
    photo_ready = pyqtSignal(object)
    finished = pyqtSignal()
    error = pyqtSignal(str)

    def __init__(self, filepaths: List[str]):
        super().__init__()
        self.filepaths = filepaths
        self._cancelled = False

    def cancel(self):
        self._cancelled = True

    def run(self):
        total = len(self.filepaths)
        for i, fp in enumerate(self.filepaths):
            if self._cancelled:
                break
            try:
                photo = extract_photo_data(fp)
                photo.phash = compute_phash(fp)
                if photo.has_gps:
                    photo.weather = fetch_weather(
                        photo.latitude, photo.longitude, photo.datetime_taken
                    )
                self.photo_ready.emit(photo)
            except Exception as e:
                self.error.emit(f"{os.path.basename(fp)}: {e}")
            self.progress.emit(i + 1, total, os.path.basename(fp))
        self.finished.emit()


# ---------------------------------------------------------------------------
# Thumbnail list item
# ---------------------------------------------------------------------------

def _make_thumbnail(filepath: str) -> QPixmap:
    """Load and crop-scale a thumbnail."""
    try:
        from PIL import Image, ImageOps
        img = Image.open(filepath)
        img = ImageOps.exif_transpose(img)
        img.thumbnail((THUMB_SIZE, THUMB_SIZE), Image.LANCZOS)

        # Convert PIL → QPixmap
        buf = img.convert("RGB").tobytes("raw", "RGB")
        qimg = QImage(buf, img.width, img.height, img.width * 3, QImage.Format.Format_RGB888)
        return QPixmap.fromImage(qimg)
    except Exception:
        pm = QPixmap(THUMB_SIZE, THUMB_SIZE)
        pm.fill(QColor("#555"))
        return pm


def _load_full_image(filepath: str, max_w: int = 900, max_h: int = 700) -> QPixmap:
    """Load full image respecting EXIF orientation, scaled to fit display."""
    try:
        from PIL import Image, ImageOps
        img = Image.open(filepath)
        img = ImageOps.exif_transpose(img)
        img.thumbnail((max_w, max_h), Image.LANCZOS)
        buf = img.convert("RGB").tobytes("raw", "RGB")
        qimg = QImage(buf, img.width, img.height, img.width * 3, QImage.Format.Format_RGB888)
        return QPixmap.fromImage(qimg)
    except Exception:
        pm = QPixmap(400, 300)
        pm.fill(QColor("#333"))
        return pm


# ---------------------------------------------------------------------------
# Upload tab
# ---------------------------------------------------------------------------

class UploadTab(QWidget):
    photos_loaded = pyqtSignal(list)   # list[PhotoData]

    def __init__(self):
        super().__init__()
        self._photos: List[PhotoData] = []
        self._worker: Optional[BatchProcessorThread] = None
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setSpacing(12)

        # Instructions
        intro = QLabel(
            "<h2>Step 1 — Upload Inspection Photos</h2>"
            "<p>Select all photos from this site inspection. EXIF metadata (GPS, timestamp, "
            "camera direction) will be extracted automatically and weather conditions fetched "
            "for each photo location.</p>"
            "<p><b>Tip:</b> Photos from iPhone, Android and Samsung are fully supported "
            "when Location Services were enabled during capture.</p>"
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        # Buttons row
        btn_row = QHBoxLayout()
        self.btn_select = QPushButton("📂  Select Photos…")
        self.btn_select.setMinimumHeight(44)
        self.btn_select.clicked.connect(self._select_photos)
        btn_row.addWidget(self.btn_select)

        self.btn_clear = QPushButton("Clear All")
        self.btn_clear.setEnabled(False)
        self.btn_clear.clicked.connect(self._clear_photos)
        btn_row.addWidget(self.btn_clear)

        btn_row.addStretch()

        self.btn_proceed = QPushButton("Proceed to Annotate  ▶")
        self.btn_proceed.setMinimumHeight(44)
        self.btn_proceed.setEnabled(False)
        self.btn_proceed.clicked.connect(lambda: self.photos_loaded.emit(self._photos))
        btn_row.addWidget(self.btn_proceed)
        layout.addLayout(btn_row)

        # Progress
        self.progress_bar = QProgressBar()
        self.progress_bar.setVisible(False)
        self.progress_label = QLabel("")
        layout.addWidget(self.progress_bar)
        layout.addWidget(self.progress_label)

        # Stats row
        self.stats_label = QLabel("")
        self.stats_label.setStyleSheet("color: #666; font-size: 13px;")
        layout.addWidget(self.stats_label)

        # File list
        self.file_list = QListWidget()
        self.file_list.setIconSize(QSize(THUMB_SIZE, THUMB_SIZE))
        self.file_list.setSpacing(4)
        layout.addWidget(self.file_list)

    def _select_photos(self):
        filepaths, _ = QFileDialog.getOpenFileNames(
            self, "Select Inspection Photos", "",
            "Images (*.jpg *.jpeg *.png *.heic *.heif *.tiff *.tif *.webp);;All Files (*)"
        )
        if not filepaths:
            return

        # Filter supported
        filepaths = [f for f in filepaths
                     if os.path.splitext(f)[1].lower() in SUPPORTED_EXTENSIONS]
        if not filepaths:
            QMessageBox.warning(self, "No Supported Photos", "No supported image files were found.")
            return

        self._photos = []
        self.file_list.clear()
        self.btn_proceed.setEnabled(False)
        self.btn_clear.setEnabled(False)
        self.btn_select.setEnabled(False)

        self.progress_bar.setVisible(True)
        self.progress_bar.setMaximum(len(filepaths))
        self.progress_bar.setValue(0)
        self.progress_label.setText("Processing photos…")

        self._worker = BatchProcessorThread(filepaths)
        self._worker.photo_ready.connect(self._on_photo_ready)
        self._worker.progress.connect(self._on_progress)
        self._worker.finished.connect(self._on_finished)
        self._worker.error.connect(lambda msg: log.warning("Error: %s", msg))
        self._worker.start()

    def _on_photo_ready(self, photo: PhotoData):
        self._photos.append(photo)

        # List item
        item = QListWidgetItem()
        thumb = _make_thumbnail(photo.file_path)
        item.setIcon(QIcon(thumb))

        flags = []
        if photo.has_gps:
            flags.append("📍GPS")
        if photo.has_direction:
            flags.append("🧭")
        if photo.weather:
            flags.append("🌤")

        label = f"{photo.filename}\n{photo.datetime_label}"
        if flags:
            label += "  " + " ".join(flags)
        item.setText(label)
        item.setData(Qt.ItemDataRole.UserRole, photo.file_path)

        self.file_list.addItem(item)

    def _on_progress(self, done: int, total: int, name: str):
        self.progress_bar.setValue(done)
        self.progress_label.setText(f"Processing {done}/{total}: {name}")

    def _on_finished(self):
        self.progress_bar.setVisible(False)
        self.progress_label.setText("")
        self.btn_select.setEnabled(True)
        self.btn_clear.setEnabled(True)

        # Run duplicate detection across all loaded photos
        detect_duplicates(self._photos)

        # Update list items for duplicates
        for i, photo in enumerate(self._photos):
            item = self.file_list.item(i)
            if item:
                if photo.is_duplicate:
                    item.setForeground(QColor("#CC0000"))
                    item.setText(item.text() + "\n⚠ DUPLICATE")
                elif photo.similar_to:
                    item.setForeground(QColor("#CC6600"))
                    item.setText(item.text() + f"\n≈ Similar to {photo.similar_to}")

        gps_count = sum(1 for p in self._photos if p.has_gps)
        dup_count = sum(1 for p in self._photos if p.is_duplicate)
        self.stats_label.setText(
            f"{len(self._photos)} photos loaded  |  "
            f"{gps_count} with GPS  |  "
            f"{dup_count} duplicate(s) detected"
        )
        if self._photos:
            self.btn_proceed.setEnabled(True)

    def _clear_photos(self):
        if self._worker and self._worker.isRunning():
            self._worker.cancel()
            self._worker.wait()
        self._photos = []
        self.file_list.clear()
        self.stats_label.setText("")
        self.btn_proceed.setEnabled(False)
        self.btn_clear.setEnabled(False)


# ---------------------------------------------------------------------------
# Annotate tab
# ---------------------------------------------------------------------------

class AnnotateTab(QWidget):
    def __init__(self):
        super().__init__()
        self._photos: List[PhotoData] = []
        self._current_idx: int = -1
        self._build_ui()

    def load_photos(self, photos: List[PhotoData]):
        self._photos = photos
        self._current_idx = -1
        self.list_widget.clear()
        for photo in photos:
            item = QListWidgetItem()
            thumb = _make_thumbnail(photo.file_path)
            item.setIcon(QIcon(thumb))
            label = f"{photo.filename}"
            if photo.is_duplicate:
                label += " ⚠DUP"
                item.setForeground(QColor("#CC0000"))
            elif photo.similar_to:
                label += " ≈SIM"
                item.setForeground(QColor("#CC6600"))
            item.setText(label)
            self.list_widget.addItem(item)

        if photos:
            self.list_widget.setCurrentRow(0)

    def _build_ui(self):
        root = QVBoxLayout(self)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        root.addWidget(splitter)

        # ---- Left panel: thumbnail list ----
        left = QWidget()
        left_lay = QVBoxLayout(left)
        left_lay.setContentsMargins(0, 0, 0, 0)

        left_lay.addWidget(QLabel("<b>Photos</b>"))

        self.list_widget = QListWidget()
        self.list_widget.setIconSize(QSize(THUMB_SIZE, THUMB_SIZE))
        self.list_widget.setMaximumWidth(200)
        self.list_widget.currentRowChanged.connect(self._navigate_to)
        left_lay.addWidget(self.list_widget)

        splitter.addWidget(left)
        splitter.setStretchFactor(0, 0)

        # ---- Centre panel: photo + nav ----
        centre = QWidget()
        centre_lay = QVBoxLayout(centre)
        centre_lay.setContentsMargins(4, 4, 4, 4)

        # Navigation row
        nav_row = QHBoxLayout()
        self.btn_prev = QPushButton("◀  Previous")
        self.btn_prev.clicked.connect(self._prev)
        self.btn_next = QPushButton("Next  ▶")
        self.btn_next.clicked.connect(self._next)
        self.counter_label = QLabel("— / —")
        self.counter_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        nav_row.addWidget(self.btn_prev)
        nav_row.addStretch()
        nav_row.addWidget(self.counter_label)
        nav_row.addStretch()
        nav_row.addWidget(self.btn_next)
        centre_lay.addLayout(nav_row)

        # Photo display
        self.photo_label = QLabel()
        self.photo_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.photo_label.setMinimumSize(500, 400)
        self.photo_label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.photo_label.setStyleSheet("background: #1a1a1a; border-radius: 6px;")
        centre_lay.addWidget(self.photo_label, stretch=1)

        # Duplicate / similar banner
        self.dup_banner = QLabel("")
        self.dup_banner.setVisible(False)
        self.dup_banner.setStyleSheet(
            "background: #ffe0e0; color: #990000; border-radius:4px; padding:6px; font-weight:bold;"
        )
        centre_lay.addWidget(self.dup_banner)

        splitter.addWidget(centre)
        splitter.setStretchFactor(1, 2)

        # ---- Right panel: metadata + notes ----
        right = QWidget()
        right_lay = QVBoxLayout(right)
        right_lay.setContentsMargins(4, 4, 4, 4)
        right.setMinimumWidth(320)
        right.setMaximumWidth(420)

        # Metadata box
        meta_group = QGroupBox("Photo Metadata")
        meta_lay = QFormLayout(meta_group)
        meta_lay.setLabelAlignment(Qt.AlignmentFlag.AlignRight)

        self.meta_datetime = QLabel("—")
        self.meta_coords = QLabel("—")
        self.meta_direction = QLabel("—")
        self.meta_altitude = QLabel("—")
        self.meta_camera = QLabel("—")
        self.meta_weather = QLabel("—")

        for label_text, widget in [
            ("Date / Time:", self.meta_datetime),
            ("Coordinates:", self.meta_coords),
            ("Direction:", self.meta_direction),
            ("Altitude:", self.meta_altitude),
            ("Camera:", self.meta_camera),
            ("Weather:", self.meta_weather),
        ]:
            widget.setWordWrap(True)
            widget.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            meta_lay.addRow(label_text, widget)

        right_lay.addWidget(meta_group)

        # Notes group
        notes_group = QGroupBox("Inspection Notes  (type or use OS dictation)")
        notes_lay = QVBoxLayout(notes_group)

        notes_lay.addWidget(QLabel("What was inspected:"))
        self.text_inspected = QTextEdit()
        self.text_inspected.setPlaceholderText("Describe what was inspected in this photo…")
        self.text_inspected.setMaximumHeight(90)
        self.text_inspected.textChanged.connect(self._save_current_notes)
        notes_lay.addWidget(self.text_inspected)

        notes_lay.addWidget(QLabel("Issues found:"))
        self.text_issues = QTextEdit()
        self.text_issues.setPlaceholderText("List any defects, non-conformances, safety concerns…")
        self.text_issues.setMaximumHeight(90)
        self.text_issues.textChanged.connect(self._save_current_notes)
        notes_lay.addWidget(self.text_issues)

        notes_lay.addWidget(QLabel("Actions required by contractor:"))
        self.text_actions = QTextEdit()
        self.text_actions.setPlaceholderText("Remediation, rectification or follow-up items…")
        self.text_actions.setMaximumHeight(90)
        self.text_actions.textChanged.connect(self._save_current_notes)
        notes_lay.addWidget(self.text_actions)

        right_lay.addWidget(notes_group)
        right_lay.addStretch()

        splitter.addWidget(right)
        splitter.setStretchFactor(2, 0)

    def _navigate_to(self, idx: int):
        if not self._photos or idx < 0 or idx >= len(self._photos):
            return

        # Save notes from old photo first
        self._save_current_notes()
        self._current_idx = idx
        photo = self._photos[idx]

        # Photo image
        pm = _load_full_image(photo.file_path,
                              max_w=self.photo_label.width() or 800,
                              max_h=self.photo_label.height() or 600)
        self.photo_label.setPixmap(pm)

        # Counter
        self.counter_label.setText(f"{idx + 1} / {len(self._photos)}")

        # Metadata
        self.meta_datetime.setText(photo.datetime_label)
        self.meta_coords.setText(photo.coords_label)
        self.meta_direction.setText(photo.direction_label)
        self.meta_altitude.setText(photo.altitude_label or "—")
        cam = f"{photo.make} {photo.model}".strip()
        self.meta_camera.setText(cam or "—")
        self.meta_weather.setText(photo.weather.summary() if photo.weather else "No data")

        # Duplicate banner
        if photo.is_duplicate:
            self.dup_banner.setText(f"⚠  DUPLICATE — very similar to '{photo.similar_to}'")
            self.dup_banner.setStyleSheet(
                "background:#ffe0e0; color:#990000; border-radius:4px; padding:6px; font-weight:bold;"
            )
            self.dup_banner.setVisible(True)
        elif photo.similar_to:
            self.dup_banner.setText(f"≈  SIMILAR to '{photo.similar_to}' — review carefully")
            self.dup_banner.setStyleSheet(
                "background:#fff3cd; color:#7a5000; border-radius:4px; padding:6px; font-weight:bold;"
            )
            self.dup_banner.setVisible(True)
        else:
            self.dup_banner.setVisible(False)

        # Notes (block signals briefly so textChanged doesn't re-save with old index)
        self.text_inspected.blockSignals(True)
        self.text_issues.blockSignals(True)
        self.text_actions.blockSignals(True)
        self.text_inspected.setPlainText(photo.what_inspected)
        self.text_issues.setPlainText(photo.issues_found)
        self.text_actions.setPlainText(photo.actions_required)
        self.text_inspected.blockSignals(False)
        self.text_issues.blockSignals(False)
        self.text_actions.blockSignals(False)

        # Nav buttons
        self.btn_prev.setEnabled(idx > 0)
        self.btn_next.setEnabled(idx < len(self._photos) - 1)

    def _save_current_notes(self):
        if self._current_idx < 0 or self._current_idx >= len(self._photos):
            return
        photo = self._photos[self._current_idx]
        photo.what_inspected = self.text_inspected.toPlainText()
        photo.issues_found = self.text_issues.toPlainText()
        photo.actions_required = self.text_actions.toPlainText()

    def _prev(self):
        if self._current_idx > 0:
            self.list_widget.setCurrentRow(self._current_idx - 1)

    def _next(self):
        if self._current_idx < len(self._photos) - 1:
            self.list_widget.setCurrentRow(self._current_idx + 1)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Left:
            self._prev()
        elif event.key() == Qt.Key.Key_Right:
            self._next()
        else:
            super().keyPressEvent(event)


# ---------------------------------------------------------------------------
# Report generation tab
# ---------------------------------------------------------------------------

class ReportTab(QWidget):
    def __init__(self):
        super().__init__()
        self._photos: List[PhotoData] = []
        self._template_path: str = ""
        self._output_dir: str = ""
        self._build_ui()

    def load_photos(self, photos: List[PhotoData]):
        self._photos = photos
        self._update_summary()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setSpacing(12)

        layout.addWidget(QLabel(
            "<h2>Step 3 — Generate Report</h2>"
            "<p>Fill in the project details, choose your Word template (optional), "
            "select an output folder and click Generate. "
            "All photos will be copied into the output folder alongside the completed report.</p>"
        ))

        # ---- Project details ----
        details_group = QGroupBox("Project Details")
        form = QFormLayout(details_group)

        self.field_site = QLineEdit()
        self.field_site.setPlaceholderText("e.g. Riverside Commercial Centre — Stage 2")
        form.addRow("Site / Project Name:", self.field_site)

        self.field_number = QLineEdit()
        self.field_number.setPlaceholderText("e.g. P2024-0142")
        form.addRow("Project Number:", self.field_number)

        self.field_inspector = QLineEdit()
        self.field_inspector.setPlaceholderText("Full name of inspector")
        form.addRow("Inspector Name:", self.field_inspector)

        self.field_address = QLineEdit()
        self.field_address.setPlaceholderText("Street address of site")
        form.addRow("Site Address:", self.field_address)

        layout.addWidget(details_group)

        # ---- Template & output ----
        files_group = QGroupBox("Files")
        files_lay = QVBoxLayout(files_group)

        tmpl_row = QHBoxLayout()
        self.lbl_template = QLabel("No template selected (default layout will be used)")
        self.lbl_template.setStyleSheet("color: #666;")
        btn_tmpl = QPushButton("Select Word Template…")
        btn_tmpl.clicked.connect(self._pick_template)
        tmpl_row.addWidget(self.lbl_template, stretch=1)
        tmpl_row.addWidget(btn_tmpl)
        files_lay.addLayout(tmpl_row)

        out_row = QHBoxLayout()
        self.lbl_output = QLabel("No output folder selected")
        self.lbl_output.setStyleSheet("color: #666;")
        btn_out = QPushButton("Select Output Folder…")
        btn_out.clicked.connect(self._pick_output)
        out_row.addWidget(self.lbl_output, stretch=1)
        out_row.addWidget(btn_out)
        files_lay.addLayout(out_row)

        layout.addWidget(files_group)

        # ---- Summary ----
        self.summary_label = QLabel("")
        self.summary_label.setStyleSheet("color: #555; font-size: 13px;")
        layout.addWidget(self.summary_label)

        # ---- Generate button ----
        btn_row = QHBoxLayout()
        btn_row.addStretch()
        self.btn_generate = QPushButton("✅  Generate Report")
        self.btn_generate.setMinimumHeight(48)
        self.btn_generate.setMinimumWidth(200)
        self.btn_generate.setStyleSheet(
            "QPushButton { background: #2d6a2d; color: white; border-radius: 6px; font-size: 15px; }"
            "QPushButton:hover { background: #3a8a3a; }"
            "QPushButton:disabled { background: #888; }"
        )
        self.btn_generate.clicked.connect(self._generate)
        btn_row.addWidget(self.btn_generate)
        btn_row.addStretch()
        layout.addLayout(btn_row)

        self.gen_progress = QProgressBar()
        self.gen_progress.setVisible(False)
        layout.addWidget(self.gen_progress)

        layout.addStretch()

        # Template placeholder help
        help_box = QGroupBox("Word Template Placeholder Reference")
        help_lay = QVBoxLayout(help_box)
        help_text = QLabel(
            "Place these tokens in your Word template and they will be replaced automatically:\n\n"
            "  <<SITE_NAME>>         — Project / site name\n"
            "  <<PROJECT_NUMBER>>    — Project reference number\n"
            "  <<INSPECTOR_NAME>>    — Inspector's full name\n"
            "  <<SITE_ADDRESS>>      — Street address of site\n"
            "  <<INSPECTION_DATE>>   — Date of earliest photo\n"
            "  <<REPORT_DATE>>       — Date report was generated\n"
            "  <<TOTAL_PHOTOS>>      — Number of photos in report\n\n"
            "Photos are always appended at the end of the document in a standard layout."
        )
        help_text.setStyleSheet("font-family: monospace; font-size: 12px;")
        help_lay.addWidget(help_text)
        layout.addWidget(help_box)

    def _update_summary(self):
        if not self._photos:
            self.summary_label.setText("No photos loaded.")
            return
        n = len(self._photos)
        dups = sum(1 for p in self._photos if p.is_duplicate)
        has_notes = sum(
            1 for p in self._photos
            if p.what_inspected or p.issues_found or p.actions_required
        )
        self.summary_label.setText(
            f"{n} photos ready for report  |  {has_notes} with notes  |  {dups} duplicate(s)"
        )

    def _pick_template(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Select Word Template", "", "Word Documents (*.docx)"
        )
        if path:
            self._template_path = path
            self.lbl_template.setText(os.path.basename(path))
            self.lbl_template.setStyleSheet("color: #228b22; font-weight: bold;")

    def _pick_output(self):
        path = QFileDialog.getExistingDirectory(self, "Select Output Folder")
        if path:
            self._output_dir = path
            self.lbl_output.setText(path)
            self.lbl_output.setStyleSheet("color: #228b22; font-weight: bold;")

    def _generate(self):
        self._update_summary()

        if not self._photos:
            QMessageBox.warning(self, "No Photos", "Please load and annotate photos first.")
            return
        if not self._output_dir:
            QMessageBox.warning(self, "No Output Folder", "Please select an output folder.")
            return

        # Create a timestamped sub-folder inside the chosen output dir
        from datetime import datetime
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        site_slug = re.sub(r"[^A-Za-z0-9_-]", "_", self.field_site.text().strip())[:40] or "inspection"
        report_folder = os.path.join(self._output_dir, f"{site_slug}_{ts}")
        os.makedirs(report_folder, exist_ok=True)

        # Copy all photos into the output folder
        photos_subdir = os.path.join(report_folder, "photos")
        os.makedirs(photos_subdir, exist_ok=True)

        self.gen_progress.setVisible(True)
        self.gen_progress.setMaximum(len(self._photos) + 1)
        self.gen_progress.setValue(0)

        for i, photo in enumerate(self._photos):
            try:
                dest = os.path.join(photos_subdir, photo.filename)
                if not os.path.exists(dest):
                    shutil.copy2(photo.file_path, dest)
            except Exception as e:
                log.warning("Could not copy %s: %s", photo.filename, e)
            self.gen_progress.setValue(i + 1)

        # Generate report
        report_path = os.path.join(report_folder, f"{site_slug}_inspection_report.docx")
        try:
            generate_report(
                photos=self._photos,
                output_path=report_path,
                template_path=self._template_path or None,
                site_name=self.field_site.text().strip(),
                project_number=self.field_number.text().strip(),
                inspector_name=self.field_inspector.text().strip(),
                site_address=self.field_address.text().strip(),
            )
        except Exception as e:
            QMessageBox.critical(self, "Report Generation Failed", str(e))
            self.gen_progress.setVisible(False)
            return

        self.gen_progress.setValue(len(self._photos) + 1)
        self.gen_progress.setVisible(False)

        # Save a JSON log of all photo metadata alongside the report
        self._save_metadata_log(report_folder)

        msg = QMessageBox(self)
        msg.setWindowTitle("Report Complete")
        msg.setText(
            f"<b>Report generated successfully!</b><br><br>"
            f"Saved to:<br><code>{report_folder}</code><br><br>"
            f"The folder contains the Word report and all {len(self._photos)} photos."
        )
        msg.setIcon(QMessageBox.Icon.Information)
        btn_open = msg.addButton("Open Folder", QMessageBox.ButtonRole.AcceptRole)
        msg.addButton("OK", QMessageBox.ButtonRole.RejectRole)
        msg.exec()

        if msg.clickedButton() == btn_open:
            import subprocess
            if sys.platform == "win32":
                subprocess.Popen(["explorer", report_folder])
            elif sys.platform == "darwin":
                subprocess.Popen(["open", report_folder])

    def _save_metadata_log(self, folder: str):
        import json
        log_path = os.path.join(folder, "photo_metadata_log.json")
        entries = []
        for p in self._photos:
            entry = {
                "filename": p.filename,
                "datetime": p.datetime_label,
                "latitude": p.latitude,
                "longitude": p.longitude,
                "altitude_m": p.altitude_m,
                "direction_degrees": p.direction_degrees,
                "direction_label": p.direction_label,
                "camera": f"{p.make} {p.model}".strip(),
                "weather": p.weather.summary() if p.weather else None,
                "is_duplicate": p.is_duplicate,
                "similar_to": p.similar_to or None,
                "what_inspected": p.what_inspected,
                "issues_found": p.issues_found,
                "actions_required": p.actions_required,
            }
            entries.append(entry)
        with open(log_path, "w", encoding="utf-8") as f:
            json.dump(entries, f, indent=2, ensure_ascii=False)


import re  # ensure available at module level for report tab


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Site Inspection Photo Processor")
        self.resize(1280, 820)

        self._photos: List[PhotoData] = []

        # Tab widget
        self.tabs = QTabWidget()
        self.tabs.setTabPosition(QTabWidget.TabPosition.North)
        self.setCentralWidget(self.tabs)

        self.upload_tab = UploadTab()
        self.annotate_tab = AnnotateTab()
        self.report_tab = ReportTab()

        self.tabs.addTab(self.upload_tab, "1 — Upload Photos")
        self.tabs.addTab(self.annotate_tab, "2 — Annotate")
        self.tabs.addTab(self.report_tab, "3 — Generate Report")

        # Connections
        self.upload_tab.photos_loaded.connect(self._on_photos_loaded)

        # Lock tabs 2 & 3 until photos are loaded
        self.tabs.setTabEnabled(1, False)
        self.tabs.setTabEnabled(2, False)

        # Status bar
        self.statusBar().showMessage("Ready — select photos to begin.")

    def _on_photos_loaded(self, photos: List[PhotoData]):
        self._photos = photos
        self.annotate_tab.load_photos(photos)
        self.report_tab.load_photos(photos)
        self.tabs.setTabEnabled(1, True)
        self.tabs.setTabEnabled(2, True)
        self.tabs.setCurrentIndex(1)
        self.statusBar().showMessage(f"{len(photos)} photos loaded. Annotate each photo, then proceed to Generate Report.")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    app = QApplication(sys.argv)
    app.setApplicationName("Site Inspection Processor")

    # Modern-ish dark-neutral style
    app.setStyleSheet("""
        QMainWindow, QWidget { font-size: 13px; }
        QGroupBox {
            font-weight: bold;
            border: 1px solid #ccc;
            border-radius: 5px;
            margin-top: 8px;
            padding-top: 8px;
        }
        QGroupBox::title {
            subcontrol-origin: margin;
            left: 10px;
            padding: 0 4px;
        }
        QPushButton {
            padding: 6px 16px;
            border-radius: 4px;
            border: 1px solid #aaa;
        }
        QPushButton:hover { background: #e8f0fe; }
        QTextEdit { border: 1px solid #ccc; border-radius: 4px; }
        QLineEdit { border: 1px solid #ccc; border-radius: 4px; padding: 4px; }
        QTabBar::tab { padding: 8px 20px; font-weight: bold; }
        QTabBar::tab:selected { border-bottom: 3px solid #2d6a2d; }
    """)

    win = MainWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
