import sys
import os
from io import BytesIO
from pathlib import Path

from PySide2.QtWidgets import (
    QApplication, QWidget, QLabel, QPushButton, QVBoxLayout, QHBoxLayout,
    QFileDialog, QMessageBox, QProgressDialog, QComboBox, QSpinBox, QTabWidget, QStyle
)
from PySide2.QtGui import QPixmap, QImage, QPainter, QPen, QColor
from PySide2.QtCore import Qt, Signal, QTimer, QSize

from PIL import Image

from image_tools import (
    find_tight_bbox,
    remove_background,
    crop_to_bbox,
    expand_bbox_to_multiple_of_4,
    expand_to_multiple_of_4,
    draw_crop_outline,
    estimate_memory_mb
)
from optimizer_utils import (
    draw_canvas_preview,
    finalize_prepared_content,
    is_supported_frames_folder,
    is_supported_image_path,
    list_frame_images,
    next_multiple_of_4,
    prepare_optimized_content,
)


def resource_path(relative):
    """Resolve path to a bundled resource, works both frozen (PyInstaller) and in dev."""
    base = getattr(sys, '_MEIPASS', os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, relative)


def pil_to_qpixmap(pil_image):
    if pil_image.mode != "RGBA":
        pil_image = pil_image.convert("RGBA")
    data = pil_image.tobytes("raw", "RGBA")
    qimage = QImage(
        data,
        pil_image.width,
        pil_image.height,
        QImage.Format_RGBA8888
    )
    return QPixmap.fromImage(qimage)


def format_file_size(num_bytes):
    size = float(max(0, num_bytes))
    units = ["B", "KB", "MB", "GB"]
    unit = units[0]
    for next_unit in units[1:]:
        if size < 1024.0:
            break
        size /= 1024.0
        unit = next_unit
    if unit == "B":
        return f"{int(size)} {unit}"
    return f"{size:.2f} {unit}"


def estimate_png_disk_size_bytes(pil_image):
    buf = BytesIO()
    pil_image.save(buf, format="PNG")
    return len(buf.getvalue())


class ManualCropLabel(QLabel):
    rectChanged = Signal(tuple)

    def __init__(self, text=""):
        super().__init__(text)
        self._pil_image = None
        self._pixmap = None
        self._manual_enabled = False
        self._rect = None
        self._drag_mode = None
        self._drag_start = None
        self._start_rect = None
        self._geom = (0, 0, 0, 0, 1.0)  # ox, oy, dw, dh, scale
        self._manual_zoom = 1.35
        self._focus_center = None  # (x, y) in image coordinates
        self._pad_limit_x = 0
        self._pad_limit_y = 0
        self._content_bbox = None  # tight visible content bbox in image coords
        self.setMouseTracking(True)

    def set_manual_zoom(self, zoom):
        self._manual_zoom = max(0.5, min(8.0, float(zoom)))
        self.update()

    def get_manual_zoom(self):
        return self._manual_zoom

    def set_manual_state(self, pil_img, rect, content_bbox=None):
        self._pil_image = pil_img
        self._pixmap = pil_to_qpixmap(pil_img)
        self._manual_enabled = True
        self._rect = tuple(int(v) for v in rect)
        self._content_bbox = tuple(content_bbox) if content_bbox is not None else None
        l, t, r, b = self._rect
        self._focus_center = ((l + r) / 2.0, (t + b) / 2.0)
        iw, ih = pil_img.size
        self._pad_limit_x = max(512, iw * 2)
        self._pad_limit_y = max(512, ih * 2)
        self.update()

    def disable_manual(self):
        self._manual_enabled = False
        self._pil_image = None
        self._pixmap = None
        self._rect = None
        self._drag_mode = None
        self._drag_start = None
        self._start_rect = None
        self._focus_center = None
        self._pad_limit_x = 0
        self._pad_limit_y = 0
        self._content_bbox = None
        self.update()

    def _is_cutting_content(self):
        if self._rect is None or self._content_bbox is None:
            return False
        l, t, r, b = self._rect
        cl, ct, cr, cb = self._content_bbox
        return l > cl or t > ct or r < cr or b < cb

    def _compute_draw_geometry(self):
        if self._pil_image is None:
            return (0, 0, 0, 0, 1.0)
        iw, ih = self._pil_image.size
        if iw <= 0 or ih <= 0:
            return (0, 0, 0, 0, 1.0)
        avail_w = max(1, self.width() - 8)
        avail_h = max(1, self.height() - 8)
        scale = min(avail_w / iw, avail_h / ih)
        if self._manual_enabled:
            scale *= self._manual_zoom
        dw = max(1, int(iw * scale))
        dh = max(1, int(ih * scale))

        if self._manual_enabled and self._focus_center is not None:
            fx, fy = self._focus_center
            ox = int((self.width() / 2.0) - (fx * scale))
            oy = int((self.height() / 2.0) - (fy * scale))
        else:
            ox = (self.width() - dw) // 2
            oy = (self.height() - dh) // 2

        return (ox, oy, dw, dh, scale)

    def _draw_checkerboard(self, painter, x, y, w, h):
        if w <= 0 or h <= 0:
            return
        check = 12
        c1 = QColor(52, 52, 52)
        c2 = QColor(32, 32, 32)
        end_x = x + w
        end_y = y + h
        for yy in range(y, end_y, check):
            for xx in range(x, end_x, check):
                painter.fillRect(
                    xx,
                    yy,
                    min(check, end_x - xx),
                    min(check, end_y - yy),
                    c1 if ((xx // check) + (yy // check)) % 2 == 0 else c2
                )

    def paintEvent(self, event):
        if not self._manual_enabled or self._pil_image is None or self._pixmap is None or self._rect is None:
            super().paintEvent(event)
            return

        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor("#1f1f1f"))

        ox, oy, dw, dh, scale = self._compute_draw_geometry()
        self._geom = (ox, oy, dw, dh, scale)

        l, t, r, b = self._rect
        x = int(ox + l * scale)
        y = int(oy + t * scale)
        w = int((r - l) * scale)
        h = int((b - t) * scale)
        sel_w = max(1, r - l)
        sel_h = max(1, b - t)
        is_mult4 = (sel_w % 4 == 0 and sel_h % 4 == 0)
        is_cutting = self._is_cutting_content()

        # Checkerboard only inside the manual crop box.
        self._draw_checkerboard(painter, x, y, max(1, w), max(1, h))

        scaled = self._pixmap.scaled(dw, dh, Qt.KeepAspectRatio, Qt.FastTransformation)
        painter.drawPixmap(ox, oy, scaled)

        # Dim outside crop box to keep focus centered.
        shade = QColor(0, 0, 0, 90)
        painter.fillRect(0, 0, self.width(), max(0, y), shade)
        painter.fillRect(0, y + h, self.width(), max(0, self.height() - (y + h)), shade)
        painter.fillRect(0, y, max(0, x), max(0, h), shade)
        painter.fillRect(x + w, y, max(0, self.width() - (x + w)), max(0, h), shade)

        frame_color = QColor(255, 60, 60) if is_cutting else QColor(0, 255, 0)
        pen = QPen(frame_color, 2)
        painter.setPen(pen)
        painter.drawRect(x, y, max(1, w), max(1, h))

        # Handles
        hs = 6
        painter.setBrush(frame_color)
        painter.drawRect(x - hs // 2, y - hs // 2, hs, hs)
        painter.drawRect(x + w - hs // 2, y - hs // 2, hs, hs)
        painter.drawRect(x - hs // 2, y + h - hs // 2, hs, hs)
        painter.drawRect(x + w - hs // 2, y + h - hs // 2, hs, hs)

        size_text = f"{sel_w} × {sel_h}"
        painter.setPen(QColor(0, 0, 0))
        painter.fillRect(x, max(0, y - 24), 120, 20, frame_color)
        painter.drawText(x + 6, max(14, y - 9), size_text)

        painter.end()

    def _to_image_pos(self, px, py):
        ox, oy, dw, dh, scale = self._geom
        if dw <= 0 or dh <= 0:
            return (0, 0)
        rx = (px - ox) / max(1e-8, scale)
        ry = (py - oy) / max(1e-8, scale)
        if self._pil_image is None:
            return (0, 0)
        iw, ih = self._pil_image.size
        rx = max(-self._pad_limit_x, min(iw + self._pad_limit_x, int(round(rx))))
        ry = max(-self._pad_limit_y, min(ih + self._pad_limit_y, int(round(ry))))
        return (rx, ry)

    def _hit_mode(self, px, py):
        if self._rect is None:
            return None
        ox, oy, _, _, scale = self._geom
        l, t, r, b = self._rect
        x1 = int(ox + l * scale)
        y1 = int(oy + t * scale)
        x2 = int(ox + r * scale)
        y2 = int(oy + b * scale)
        m = 8

        near_l = abs(px - x1) <= m
        near_r = abs(px - x2) <= m
        near_t = abs(py - y1) <= m
        near_b = abs(py - y2) <= m
        inside = (x1 < px < x2 and y1 < py < y2)

        if near_l and near_t:
            return "tl"
        if near_r and near_t:
            return "tr"
        if near_l and near_b:
            return "bl"
        if near_r and near_b:
            return "br"
        if near_l and y1 <= py <= y2:
            return "l"
        if near_r and y1 <= py <= y2:
            return "r"
        if near_t and x1 <= px <= x2:
            return "t"
        if near_b and x1 <= px <= x2:
            return "b"
        return None

    def mousePressEvent(self, event):
        if not self._manual_enabled or self._pil_image is None or self._rect is None:
            super().mousePressEvent(event)
            return
        if event.button() != Qt.LeftButton:
            return
        mode = self._hit_mode(event.x(), event.y())
        if mode is None:
            return
        self._drag_mode = mode
        self._drag_start = self._to_image_pos(event.x(), event.y())
        self._start_rect = self._rect

    def mouseMoveEvent(self, event):
        if not self._manual_enabled or self._pil_image is None or self._rect is None:
            super().mouseMoveEvent(event)
            return

        if self._drag_mode is None or self._drag_start is None or self._start_rect is None:
            mode = self._hit_mode(event.x(), event.y())
            if mode in {"l", "r", "t", "b", "tl", "tr", "bl", "br"}:
                self.setCursor(Qt.CrossCursor)
            else:
                self.setCursor(Qt.ArrowCursor)
            return

        cx, cy = self._to_image_pos(event.x(), event.y())
        sx, sy = self._drag_start
        dx = cx - sx
        dy = cy - sy

        l, t, r, b = self._start_rect
        iw, ih = self._pil_image.size
        min_size = 4

        if self._drag_mode == "move":
            w = r - l
            h = b - t
            min_l = -self._pad_limit_x
            max_l = iw + self._pad_limit_x - w
            min_t = -self._pad_limit_y
            max_t = ih + self._pad_limit_y - h
            nl = max(min_l, min(max_l, l + dx))
            nt = max(min_t, min(max_t, t + dy))
            nr = nl + w
            nb = nt + h
        else:
            nl, nt, nr, nb = l, t, r, b
            min_l = -self._pad_limit_x
            max_r = iw + self._pad_limit_x
            min_t = -self._pad_limit_y
            max_b = ih + self._pad_limit_y
            if "l" in self._drag_mode:
                nl = max(min_l, min(r - min_size, l + dx))
            if "r" in self._drag_mode:
                nr = min(max_r, max(l + min_size, r + dx))
            if "t" in self._drag_mode:
                nt = max(min_t, min(b - min_size, t + dy))
            if "b" in self._drag_mode:
                nb = min(max_b, max(t + min_size, b + dy))

        new_rect = (int(nl), int(nt), int(nr), int(nb))
        if new_rect != self._rect:
            self._rect = new_rect
            l2, t2, r2, b2 = self._rect
            self._focus_center = ((l2 + r2) / 2.0, (t2 + b2) / 2.0)
            self.rectChanged.emit(self._rect)
            self.update()

    def mouseReleaseEvent(self, event):
        self._drag_mode = None
        self._drag_start = None
        self._start_rect = None
        self.setCursor(Qt.ArrowCursor)
        super().mouseReleaseEvent(event)


class TextureOptimizerUI(QWidget):
    ALLOWED_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".bmp"}
    MULTI_PLAYBACK_INTERVAL_MS = 33

    ALERT_INFO_STYLE = (
        "color: #ffb3b3;"
        "font-weight: bold;"
        "padding: 6px;"
        "border: 1px solid #cc4444;"
        "border-radius: 4px;"
        "background-color: #3b0000;"
        "margin-top: 8px;"
    )

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Texture Memory Optimizer")
        self.setWindowFlag(Qt.WindowMaximizeButtonHint, False)
        self.setAcceptDrops(True)
        self.setStyleSheet(open(resource_path("style.qss")).read())

        self.original_path = None
        self.original_image = None
        self.cropped_image = None
        self.manual_base_image = None
        self.manual_rect = None
        self.multi_folder = None
        self.multi_frame_paths = []
        self.multi_auto_target_size = None
        self.multi_max_content_size = None
        self.multi_current_original_mem = 0.0
        self.multi_source_cache = {}
        self.multi_prepared_cache = {}
        self.multi_preview_cache = {}
        self.multi_player_timer = QTimer(self)
        self.multi_player_timer.timeout.connect(self._advance_multi_frame)

        self.init_ui()

    def init_ui(self):
        # Original panel
        self.original_title = QLabel("Original Image:")
        self.original_title.setObjectName("panelTitle")

        self.original_preview = QLabel("No image loaded")
        self.original_preview.setAlignment(Qt.AlignCenter)
        self.original_preview.setObjectName("imagePreview")

        self.original_info = QLabel("Size: -\nRaw Memory: -\nDisk Size: -")
        self.original_info.setObjectName("infoLabel")

        original_layout = QVBoxLayout()
        original_layout.addWidget(self.original_title)
        original_layout.addWidget(self.original_preview)
        original_layout.addWidget(self.original_info)

        # Optimized panel
        self.cropped_title = QLabel("Optimized Image:")
        self.cropped_title.setObjectName("panelTitle")

        # Mode dropdown next to the Optimized title
        self.mode_combo = QComboBox()
        self.mode_combo.addItems(["Auto", "Manual", "128 × 128", "256 × 256", "512 × 512", "1024 × 1024", "2048 × 2048", "Custom..."])
        self.mode_combo.setObjectName("sizeCombo")
        self.mode_combo.setFixedWidth(130)
        self.mode_combo.currentTextChanged.connect(self._on_mode_changed)

        # Custom W × H row (only visible in Custom mode)
        self.custom_w_spin = QSpinBox()
        self.custom_w_spin.setRange(4, 8192)
        self.custom_w_spin.setSingleStep(4)
        self.custom_w_spin.setValue(512)
        self.custom_w_spin.setPrefix("W: ")
        self.custom_w_spin.setFixedWidth(82)
        self.custom_w_spin.setObjectName("customSpin")
        self.custom_w_spin.setKeyboardTracking(False)

        custom_x_label = QLabel("×")
        custom_x_label.setFixedWidth(14)
        custom_x_label.setAlignment(Qt.AlignCenter)

        self.custom_h_spin = QSpinBox()
        self.custom_h_spin.setRange(4, 8192)
        self.custom_h_spin.setSingleStep(4)
        self.custom_h_spin.setValue(512)
        self.custom_h_spin.setPrefix("H: ")
        self.custom_h_spin.setFixedWidth(82)
        self.custom_h_spin.setObjectName("customSpin")
        self.custom_h_spin.setKeyboardTracking(False)

        self.custom_apply_btn = QPushButton("Update")
        self.custom_apply_btn.clicked.connect(self._apply_custom_size)
        self.custom_apply_btn.setEnabled(False)

        custom_row_layout = QHBoxLayout()
        custom_row_layout.setContentsMargins(0, 0, 0, 0)
        custom_row_layout.addStretch()
        custom_row_layout.addWidget(self.custom_w_spin)
        custom_row_layout.addWidget(custom_x_label)
        custom_row_layout.addWidget(self.custom_h_spin)
        custom_row_layout.addWidget(self.custom_apply_btn)

        self.custom_row_widget = QWidget()
        self.custom_row_widget.setLayout(custom_row_layout)
        self.custom_row_widget.setVisible(False)

        # Manual zoom row (only visible in Manual mode)
        self.manual_zoom_out_btn = QPushButton("-")
        self.manual_zoom_out_btn.setFixedWidth(28)
        self.manual_zoom_out_btn.clicked.connect(self._manual_zoom_out)
        self.manual_zoom_out_btn.setEnabled(False)

        self.manual_zoom_label = QLabel("Zoom: 135%")
        self.manual_zoom_label.setAlignment(Qt.AlignCenter)

        self.manual_zoom_in_btn = QPushButton("+")
        self.manual_zoom_in_btn.setFixedWidth(28)
        self.manual_zoom_in_btn.clicked.connect(self._manual_zoom_in)
        self.manual_zoom_in_btn.setEnabled(False)

        manual_zoom_layout = QHBoxLayout()
        manual_zoom_layout.setContentsMargins(0, 0, 0, 0)
        manual_zoom_layout.addStretch()
        manual_zoom_layout.addWidget(self.manual_zoom_out_btn)
        manual_zoom_layout.addWidget(self.manual_zoom_label)
        manual_zoom_layout.addWidget(self.manual_zoom_in_btn)

        self.manual_zoom_widget = QWidget()
        self.manual_zoom_widget.setLayout(manual_zoom_layout)
        self.manual_zoom_widget.setVisible(False)

        optimized_header = QHBoxLayout()
        optimized_header.addWidget(self.cropped_title)
        optimized_header.addStretch()
        optimized_header.addWidget(self.mode_combo)

        optimized_header_col = QVBoxLayout()
        optimized_header_col.setSpacing(2)
        optimized_header_col.addLayout(optimized_header)
        optimized_header_col.addWidget(self.custom_row_widget)
        optimized_header_col.addWidget(self.manual_zoom_widget)

        self.cropped_preview = ManualCropLabel("No crop yet")
        self.cropped_preview.setAlignment(Qt.AlignCenter)
        self.cropped_preview.setObjectName("imagePreview")
        self.cropped_preview.rectChanged.connect(self._on_manual_rect_changed)

        self.cropped_info = QLabel("Size: -\nRaw Memory: -\nDisk Size: -\nSaved: -")
        self.cropped_info.setObjectName("infoLabel")

        cropped_layout = QVBoxLayout()
        cropped_layout.addLayout(optimized_header_col)
        cropped_layout.addWidget(self.cropped_preview)
        cropped_layout.addWidget(self.cropped_info)

        # Summary
        self.summary_label = QLabel("Select an image to begin.")
        self.summary_label.setAlignment(Qt.AlignCenter)
        self.summary_label.setObjectName("summaryLabel")

        # Buttons
        self.select_btn = QPushButton("Select Image")
        self.select_btn.clicked.connect(self.open_file_dialog)

        self.export_btn = QPushButton("Export")
        self.export_btn.clicked.connect(self.handle_export)
        self.export_btn.setObjectName("exportButton")
        self.export_btn.setEnabled(False)

        self.mode_combo.setEnabled(False)
        self.custom_w_spin.setEnabled(False)
        self.custom_h_spin.setEnabled(False)

        button_layout = QHBoxLayout()
        button_layout.addWidget(self.select_btn)
        button_layout.addWidget(self.export_btn)

        # --- Single Frame page ---
        images_layout = QHBoxLayout()
        images_layout.addLayout(original_layout)
        images_layout.addLayout(cropped_layout)

        single_page_layout = QVBoxLayout()
        single_page_layout.setContentsMargins(8, 8, 8, 8)
        single_page_layout.addLayout(images_layout)
        single_page_layout.addWidget(self.summary_label)
        single_page_layout.addLayout(button_layout)

        single_frame_widget = QWidget()
        single_frame_widget.setObjectName("singleFramePage")
        single_frame_widget.setLayout(single_page_layout)

        # --- Multi Frame page ---
        self.multi_frame_combo = QComboBox()
        self.multi_frame_combo.setObjectName("sizeCombo")
        self.multi_frame_combo.setMinimumWidth(220)
        self.multi_frame_combo.setEnabled(False)
        self.multi_frame_combo.currentIndexChanged.connect(self.refresh_multi_frame_preview)

        self.multi_mode_combo = QComboBox()
        self.multi_mode_combo.setObjectName("sizeCombo")
        self.multi_mode_combo.setFixedWidth(130)
        self.multi_mode_combo.addItems(["Auto", "128 × 128", "256 × 256", "512 × 512", "1024 × 1024", "2048 × 2048"])
        self.multi_mode_combo.setEnabled(False)
        self.multi_mode_combo.currentTextChanged.connect(self.refresh_multi_frame_preview)

        self.multi_prev_btn = QPushButton()
        self.multi_prev_btn.setIcon(self.style().standardIcon(QStyle.SP_MediaSeekBackward))
        self.multi_prev_btn.setIconSize(QSize(36, 36))
        self.multi_prev_btn.setToolTip("Previous frame")
        self.multi_prev_btn.setAccessibleName("Previous frame")
        self.multi_prev_btn.setFixedSize(44, 44)
        self.multi_prev_btn.setEnabled(False)
        self.multi_prev_btn.clicked.connect(self.go_to_prev_multi_frame)

        self.multi_play_btn = QPushButton()
        self.multi_play_btn.setIcon(self.style().standardIcon(QStyle.SP_MediaPlay))
        self.multi_play_btn.setIconSize(QSize(36, 36))
        self.multi_play_btn.setToolTip("Play optimized preview")
        self.multi_play_btn.setAccessibleName("Play optimized preview")
        self.multi_play_btn.setFixedSize(44, 44)
        self.multi_play_btn.setEnabled(False)
        self.multi_play_btn.clicked.connect(self.start_multi_playback)

        self.multi_stop_btn = QPushButton()
        self.multi_stop_btn.setIcon(self.style().standardIcon(QStyle.SP_MediaStop))
        self.multi_stop_btn.setIconSize(QSize(36, 36))
        self.multi_stop_btn.setToolTip("Stop optimized preview")
        self.multi_stop_btn.setAccessibleName("Stop optimized preview")
        self.multi_stop_btn.setFixedSize(44, 44)
        self.multi_stop_btn.setEnabled(False)
        self.multi_stop_btn.clicked.connect(self.stop_multi_playback)

        self.multi_next_btn = QPushButton()
        self.multi_next_btn.setIcon(self.style().standardIcon(QStyle.SP_MediaSeekForward))
        self.multi_next_btn.setIconSize(QSize(36, 36))
        self.multi_next_btn.setToolTip("Next frame")
        self.multi_next_btn.setAccessibleName("Next frame")
        self.multi_next_btn.setFixedSize(44, 44)
        self.multi_next_btn.setEnabled(False)
        self.multi_next_btn.clicked.connect(self.go_to_next_multi_frame)

        self.multi_frame_counter = QLabel("- / -")
        self.multi_frame_counter.setObjectName("infoLabel")
        self.multi_frame_counter.setAlignment(Qt.AlignCenter)
        self.multi_frame_counter.setMinimumWidth(120)

        self.multi_original_preview = QLabel("No frame loaded")
        self.multi_original_preview.setAlignment(Qt.AlignCenter)
        self.multi_original_preview.setObjectName("imagePreview")

        self.multi_original_info = QLabel("Size: -\nRaw Memory: -\nDisk Size: -")
        self.multi_original_info.setObjectName("originalInfoLabel")

        self.multi_cropped_preview = QLabel("No optimized frame yet")
        self.multi_cropped_preview.setAlignment(Qt.AlignCenter)
        self.multi_cropped_preview.setObjectName("imagePreview")

        self.multi_cropped_info = QLabel("Size: -\nRaw Memory: -\nDisk Size: -\nSaved: -")
        self.multi_cropped_info.setObjectName("infoLabel")

        # Header: all on one line
        multi_right_header = QHBoxLayout()
        multi_right_header.addStretch()
        multi_right_header.addWidget(QLabel("Frame:"))
        multi_right_header.addWidget(self.multi_frame_combo)
        multi_right_header.addSpacing(8)
        multi_right_header.addWidget(self.multi_mode_combo)
        multi_right_header.addStretch()

        multi_right = QVBoxLayout()
        multi_right.addLayout(multi_right_header)

        multi_preview_row = QHBoxLayout()
        multi_preview_row.addWidget(self.multi_prev_btn)
        multi_preview_row.addWidget(self.multi_cropped_preview)
        multi_preview_row.addWidget(self.multi_next_btn)

        multi_controls_row = QHBoxLayout()
        multi_controls_row.addStretch()
        multi_controls_row.addWidget(self.multi_play_btn)
        multi_controls_row.addWidget(self.multi_stop_btn)
        multi_controls_row.addSpacing(8)
        multi_controls_row.addWidget(self.multi_frame_counter)
        multi_controls_row.addStretch()

        multi_info_panels_row = QHBoxLayout()
        multi_info_panels_row.addWidget(self.multi_original_info)
        multi_info_panels_row.addWidget(self.multi_cropped_info)

        multi_right.addLayout(multi_preview_row)
        multi_right.addLayout(multi_controls_row)
        multi_right.addLayout(multi_info_panels_row)

        self.multi_info_label = QLabel("Select a folder to load all frames.")
        self.multi_info_label.setObjectName("summaryLabel")
        self.multi_info_label.setAlignment(Qt.AlignCenter)

        self.multi_select_btn = QPushButton("Select Folder")
        self.multi_select_btn.clicked.connect(self.open_multi_images)

        self.multi_export_frame_btn = QPushButton("Export Frame")
        self.multi_export_frame_btn.setObjectName("exportButton")
        self.multi_export_frame_btn.setEnabled(False)
        self.multi_export_frame_btn.clicked.connect(self.export_selected_multi_frame)

        self.multi_export_all_btn = QPushButton("Export All Frames")
        self.multi_export_all_btn.setObjectName("exportButton")
        self.multi_export_all_btn.setEnabled(False)
        self.multi_export_all_btn.clicked.connect(self.export_all_multi_frames)

        multi_buttons = QHBoxLayout()
        multi_buttons.addWidget(self.multi_select_btn)
        multi_buttons.addWidget(self.multi_export_frame_btn)
        multi_buttons.addWidget(self.multi_export_all_btn)

        multi_frame_layout = QVBoxLayout()
        multi_frame_layout.setContentsMargins(8, 8, 8, 8)
        multi_frame_layout.addLayout(multi_right)
        multi_frame_layout.addWidget(self.multi_info_label)
        multi_frame_layout.addLayout(multi_buttons)

        multi_frame_widget = QWidget()
        multi_frame_widget.setObjectName("multiFramePage")
        multi_frame_widget.setLayout(multi_frame_layout)

        # --- Tab widget ---
        self.tab_widget = QTabWidget()
        self.tab_widget.addTab(single_frame_widget, "Single Frame")
        self.tab_widget.addTab(multi_frame_widget, "Multi Frame")

        outer_layout = QVBoxLayout()
        outer_layout.setContentsMargins(0, 0, 0, 0)
        outer_layout.addWidget(self.tab_widget)
        self.setLayout(outer_layout)

    def dragEnterEvent(self, event):
        if not event.mimeData().hasUrls():
            event.ignore()
            return

        dropped_paths = self._get_dropped_local_paths(event)
        if any(is_supported_image_path(path, self.ALLOWED_IMAGE_SUFFIXES) for path in dropped_paths):
            event.acceptProposedAction()
            return

        if any(is_supported_frames_folder(path, self.ALLOWED_IMAGE_SUFFIXES) for path in dropped_paths):
            event.acceptProposedAction()
            return

        event.ignore()

    def dropEvent(self, event):
        dropped_paths = self._get_dropped_local_paths(event)

        folder_path = next((
            path for path in dropped_paths
            if is_supported_frames_folder(path, self.ALLOWED_IMAGE_SUFFIXES)
        ), None)
        if folder_path is not None:
            self.tab_widget.setCurrentIndex(1)
            self._load_multi_folder(folder_path)
            event.acceptProposedAction()
            return

        image_path = next((
            path for path in dropped_paths
            if is_supported_image_path(path, self.ALLOWED_IMAGE_SUFFIXES)
        ), None)

        if image_path is None:
            QMessageBox.information(
                self,
                "Unsupported Drop",
                "Drop a supported image file or a folder containing image frames (.png, .jpg, .jpeg, .bmp)."
            )
            event.ignore()
            return

        self.tab_widget.setCurrentIndex(0)
        self.load_original_image(str(image_path))
        event.acceptProposedAction()

    def _get_dropped_local_paths(self, event):
        paths = []
        for url in event.mimeData().urls():
            if url.isLocalFile():
                paths.append(Path(url.toLocalFile()))
        return paths

    # ---------------------------
    # Mode dropdown helpers
    # ---------------------------
    def _on_mode_changed(self, text):
        self.custom_row_widget.setVisible(text == "Custom...")
        self.custom_w_spin.setEnabled(text == "Custom..." and self.original_image is not None)
        self.custom_h_spin.setEnabled(text == "Custom..." and self.original_image is not None)
        self.custom_apply_btn.setEnabled(text == "Custom..." and self.original_image is not None)
        self.manual_zoom_widget.setVisible(text == "Manual")
        self.manual_zoom_out_btn.setEnabled(text == "Manual" and self.original_image is not None)
        self.manual_zoom_in_btn.setEnabled(text == "Manual" and self.original_image is not None)

        if text != "Manual":
            self.cropped_preview.disable_manual()

        if text == "Manual" and self.original_image is not None:
            self.summary_label.setText("Manual mode: zoomed view + checkerboard. Drag the green rectangle; size updates live.")
            self.start_manual_crop()
            return

        if text == "Custom..." and self.original_image is not None:
            self.summary_label.setText("Set custom size and press Update.")
        if self.original_image is not None and text != "Custom...":
            self.handle_auto_crop()

    def _apply_custom_size(self):
        if self.mode_combo.currentText() == "Custom..." and self.original_image is not None:
            self.handle_auto_crop()

    def _update_manual_zoom_ui(self):
        self.manual_zoom_label.setText(f"Zoom: {int(self.cropped_preview.get_manual_zoom() * 100)}%")

    def _manual_zoom_in(self):
        self.cropped_preview.set_manual_zoom(self.cropped_preview.get_manual_zoom() * 1.15)
        self._update_manual_zoom_ui()

    def _manual_zoom_out(self):
        self.cropped_preview.set_manual_zoom(self.cropped_preview.get_manual_zoom() / 1.15)
        self._update_manual_zoom_ui()

    def get_target_canvas_size(self):
        """Returns (width, height) or None for Auto (tight crop)."""
        text = self.mode_combo.currentText()
        if text in ("Auto", "Manual"):
            return None
        if text == "Custom...":
            return (self.custom_w_spin.value(), self.custom_h_spin.value())
        n = int(text.split("×")[0].strip())
        return (n, n)

    def get_multi_target_canvas_size(self):
        text = self.multi_mode_combo.currentText()
        if text == "Auto":
            return None
        n = int(text.split("×")[0].strip())
        return (n, n)

    # ---------------------------
    # File selection
    # ---------------------------
    def open_file_dialog(self):
        file_path, _ = QFileDialog.getOpenFileName(
            self,
            "Select Image",
            "",
            "Images (*.png *.jpg *.jpeg *.bmp)"
        )
        if not file_path:
            return

        self.load_original_image(file_path)

    def load_original_image(self, path):
        try:
            img = Image.open(path)
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to load image:\n{e}")
            return

        self.original_path = path
        self.original_image = img
        self.cropped_image = None
        self.manual_base_image = None
        self.manual_rect = None
        self.mode_combo.setEnabled(True)
        self.custom_w_spin.setEnabled(self.mode_combo.currentText() == "Custom...")
        self.custom_h_spin.setEnabled(self.mode_combo.currentText() == "Custom...")
        self.custom_apply_btn.setEnabled(self.mode_combo.currentText() == "Custom...")
        self.manual_zoom_out_btn.setEnabled(self.mode_combo.currentText() == "Manual")
        self.manual_zoom_in_btn.setEnabled(self.mode_combo.currentText() == "Manual")
        self.export_btn.setEnabled(False)

        pixmap = pil_to_qpixmap(img)
        self.original_preview.setPixmap(
            pixmap.scaled(300, 300, Qt.KeepAspectRatio)
        )

        w, h = img.size
        mem = estimate_memory_mb(w, h)

        # Detect how much of the source texture is actually used.
        # If there is too much empty area, mark this panel in red.
        bbox = find_tight_bbox(img, tolerance=20)
        if bbox is None:
            fill_pct = 0.0
        else:
            bw = max(0, bbox[2] - bbox[0])
            bh = max(0, bbox[3] - bbox[1])
            fill_pct = (bw * bh) / (w * h) if (w * h) > 0 else 0.0

        empty_pct = (1.0 - fill_pct) * 100.0
        is_unoptimized = empty_pct >= 50.0

        if is_unoptimized:
            self.original_info.setStyleSheet(self.ALERT_INFO_STYLE)
            status = "Status: Unoptimized"
        else:
            # Use default QSS style (green) for okay textures.
            self.original_info.setStyleSheet("")
            status = "Status: Good"

        self.original_info.setText(
            f"Size: {w}×{h}\n"
            f"Raw Memory: {mem:.2f} MB\n"
            f"Disk Size: {format_file_size(os.path.getsize(path))}\n"
            f"Empty: {empty_pct:.1f}%\n"
            f"{status}"
        )

        self.cropped_preview.setText("No crop yet")
        self.cropped_info.setText("Size: -\nRaw Memory: -\nDisk Size: -\nSaved: -")
        if self.mode_combo.currentText() == "Custom...":
            self.summary_label.setText("Image loaded. Set custom size and press Update.")
        elif self.mode_combo.currentText() == "Manual":
            self.summary_label.setText("Image loaded. Starting Manual mode...")
        else:
            self.summary_label.setText("Image loaded. Running Auto crop...")
        QApplication.processEvents()

        # Auto-run crop immediately on load, except Custom mode which waits for Update.
        if self.mode_combo.currentText() == "Manual":
            self.start_manual_crop()
        elif self.mode_combo.currentText() != "Custom...":
            self.handle_auto_crop()

    def start_manual_crop(self):
        if self.original_image is None:
            return

        progress = QProgressDialog("Preparing manual crop...", None, 0, 100, self)
        progress.setWindowTitle("Manual Crop")
        progress.setWindowModality(Qt.WindowModal)
        progress.setMinimumDuration(0)
        progress.setAutoClose(True)
        progress.setValue(0)
        QApplication.processEvents()

        try:
            img = self.original_image.convert("RGBA")
            progress.setValue(20)
            QApplication.processEvents()

            progress.setLabelText("Removing background...")
            progress.setValue(40)
            QApplication.processEvents()
            img = remove_background(img)

            progress.setLabelText("Finding initial crop area...")
            progress.setValue(80)
            QApplication.processEvents()
            tight_bbox = img.split()[3].getbbox()
            if tight_bbox is None:
                progress.close()
                QMessageBox.information(self, "Info", "No visible pixels found.")
                return

            # Start Manual mode from auto-crop bounds, but keep the FULL image
            # as editable area so users can expand crop wider/taller as needed.
            bbox = tight_bbox
            bbox = expand_bbox_to_multiple_of_4(bbox, img.size)

            l, t, r, b = bbox
            bw = max(1, r - l)
            bh = max(1, b - t)
            margin_x = max(8, bw // 4)
            margin_y = max(8, bh // 4)

            # Slightly wider initial rectangle for easier first adjustment.
            il, it = 0, 0
            ir, ib = img.size
            init_rect = (
                max(il, l - margin_x),
                max(it, t - margin_y),
                min(ir, r + margin_x),
                min(ib, b + margin_y),
            )

            self.manual_base_image = img
            self.manual_rect = init_rect
            self.cropped_preview.set_manual_state(img, init_rect, content_bbox=tight_bbox)
            self._update_manual_zoom_ui()
            self._on_manual_rect_changed(init_rect)

            progress.setValue(100)
            QApplication.processEvents()
        finally:
            progress.close()

    def _on_manual_rect_changed(self, rect):
        if self.mode_combo.currentText() != "Manual":
            return
        if self.manual_base_image is None:
            return

        self.manual_rect = tuple(int(v) for v in rect)
        l, t, r, b = self.manual_rect
        if r <= l or b <= t:
            return

        src = self.manual_base_image
        iw, ih = src.size
        out_w = r - l
        out_h = b - t
        cropped = Image.new("RGBA", (out_w, out_h), (0, 0, 0, 0))

        ix1 = max(0, l)
        iy1 = max(0, t)
        ix2 = min(iw, r)
        iy2 = min(ih, b)
        if ix2 > ix1 and iy2 > iy1:
            part = crop_to_bbox(src, (ix1, iy1, ix2, iy2))
            paste_x = ix1 - l
            paste_y = iy1 - t
            cropped.paste(part, (paste_x, paste_y), part)

        final = expand_to_multiple_of_4(cropped)
        self.cropped_image = final
        self.export_btn.setEnabled(True)

        ow, oh = self.original_image.size
        orig_mem = estimate_memory_mb(ow, oh)
        fw, fh = final.size
        final_mem = estimate_memory_mb(fw, fh)
        saved = orig_mem - final_mem
        saved_pct = (saved / orig_mem) * 100 if orig_mem > 0 else 0

        cw, ch = cropped.size
        self.cropped_info.setText(
            f"Manual: {cw}×{ch}\n"
            f"Final: {fw}×{fh}\n"
            f"Raw Memory: {final_mem:.2f} MB\n"
            f"Disk Size: (updates on export)\n"
            f"Saved: {saved:.2f} MB ({saved_pct:.1f}%)"
        )
        if (cw % 4) != 0 or (ch % 4) != 0:
            self.summary_label.setText(f"⚠ Manual selection: {cw}×{ch} (not multiple of 4)")
        else:
            self.summary_label.setText(f"Manual selection: {cw}×{ch} (drag green box)")

    # ---------------------------
    # Auto crop
    # ---------------------------
    def handle_auto_crop(self):
        if self.original_image is None:
            return

        progress = QProgressDialog("Preparing image...", None, 0, 100, self)
        progress.setWindowTitle("Optimizing Texture")
        progress.setWindowModality(Qt.WindowModal)
        progress.setMinimumDuration(0)
        progress.setAutoClose(True)
        progress.setValue(0)
        QApplication.processEvents()

        try:
            img = self.original_image.convert("RGBA")
            progress.setValue(5)
            QApplication.processEvents()

            progress.setLabelText("Removing background...")
            progress.setValue(10)
            QApplication.processEvents()

            last_bg_progress = {"value": -1}

            def on_remove_progress(pct):
                mapped = 10 + int((max(0, min(100, pct)) * 70) / 100)
                if mapped != last_bg_progress["value"]:
                    last_bg_progress["value"] = mapped
                    progress.setValue(mapped)
                    QApplication.processEvents()

            # 1. Remove background via flood-fill → transparent pixels
            img = remove_background(img, progress_callback=on_remove_progress)

            progress.setLabelText("Finding visible bounds...")
            progress.setValue(85)
            QApplication.processEvents()

            # 2. Find tight bbox from alpha channel (non-zero alpha = content)
            bbox = img.split()[3].getbbox()

            if bbox is None:
                progress.close()
                QMessageBox.information(self, "Info", "No visible pixels found.")
                return

            progress.setLabelText("Adjusting bounds to multiple of 4...")
            progress.setValue(90)
            QApplication.processEvents()

            # 3. Expand the bbox outward to a multiple of 4 before cropping.
            bbox = expand_bbox_to_multiple_of_4(bbox, img.size)

            progress.setLabelText("Cropping image...")
            progress.setValue(94)
            QApplication.processEvents()

            # 4. Crop
            cropped = crop_to_bbox(img, bbox)

            progress.setLabelText("Finalizing texture size...")
            progress.setValue(97)
            QApplication.processEvents()

            # 5. Pad to target canvas size if selected, otherwise nearest mult-of-4.
            target = self.get_target_canvas_size()
            if target is not None:
                tw, th = target
                cw, ch = cropped.size
                if cw > tw or ch > th:
                    progress.close()
                    QMessageBox.warning(
                        self, "Too Large",
                        f"Cropped content ({cw}×{ch}) is larger than target ({tw}×{th}).\n"
                        "Use a larger target size or choose Auto."
                    )
                    return
                canvas = Image.new("RGBA", (tw, th), (0, 0, 0, 0))
                offset_x = (tw - cw) // 2
                offset_y = (th - ch) // 2
                canvas.paste(cropped, (offset_x, offset_y))
                final = canvas
                # Canvas visualization: checkerboard shows empty space, content placed inside
                preview_img = draw_canvas_preview(cropped, tw, th)
            else:
                final = expand_to_multiple_of_4(cropped)
                # Auto mode: plain outlined preview
                preview_img = draw_crop_outline(final, cropped)

            # Save CLEAN version for export
            self.cropped_image = final
            self.export_btn.setEnabled(True)

            # Show preview
            if target is not None:
                # Already at display scale
                self.cropped_preview.setPixmap(pil_to_qpixmap(preview_img))
            else:
                self.cropped_preview.setPixmap(
                    pil_to_qpixmap(preview_img).scaled(300, 300, Qt.KeepAspectRatio)
                )

            # Memory calculations
            ow, oh = self.original_image.size
            orig_mem = estimate_memory_mb(ow, oh)

            fw, fh = final.size
            final_mem = estimate_memory_mb(fw, fh)

            saved = orig_mem - final_mem
            saved_pct = (saved / orig_mem) * 100 if orig_mem > 0 else 0

            if target is None:
                disk_est = format_file_size(estimate_png_disk_size_bytes(final))
                self.cropped_info.setText(
                    f"Final: {fw}×{fh}\n"
                    f"Raw Memory: {final_mem:.2f} MB\n"
                    f"Disk Size (PNG est.): {disk_est}\n"
                    f"Saved: {saved:.2f} MB ({saved_pct:.1f}%)"
                )
            else:
                disk_est = format_file_size(estimate_png_disk_size_bytes(final))
                self.cropped_info.setText(
                    f"Content: {cropped.size[0]}×{cropped.size[1]}\n"
                    f"Final: {fw}×{fh}\n"
                    f"Raw Memory: {final_mem:.2f} MB\n"
                    f"Disk Size (PNG est.): {disk_est}\n"
                    f"Saved: {saved:.2f} MB ({saved_pct:.1f}%)"
                )

            self.summary_label.setText(
                f"Optimized from {orig_mem:.2f} MB → {final_mem:.2f} MB"
            )

            progress.setLabelText("Done")
            progress.setValue(100)
            QApplication.processEvents()
        finally:
            progress.close()

    # ---------------------------
    # Export
    # ---------------------------
    def handle_export(self):
        if self.cropped_image is None:
            QMessageBox.information(self, "Info", "No optimized image yet.")
            return

        default_name = "optimized.png"
        if self.original_path:
            p = Path(self.original_path)
            default_name = f"{p.stem}_optimized{p.suffix}"

        save_path, _ = QFileDialog.getSaveFileName(
            self,
            "Save Optimized Image",
            default_name,
            "Images (*.png *.jpg *.jpeg *.bmp)"
        )
        if not save_path:
            return

        try:
            self.cropped_image.save(save_path)
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to save:\n{e}")
            return

        disk_size = format_file_size(os.path.getsize(save_path)) if os.path.exists(save_path) else "Unknown"

        QMessageBox.information(
            self,
            "Saved",
            f"Saved to:\n{save_path}\n\nActual File Size: {disk_size}"
        )

    # ---------------------------
    # Multi frame
    # ---------------------------
    def open_multi_images(self):
        folder_path = QFileDialog.getExistingDirectory(
            self,
            "Select Frames Folder",
            ""
        )
        if not folder_path:
            return

        self._load_multi_folder(Path(folder_path))

    def _load_multi_folder(self, folder):
        folder = Path(folder)

        frames = list_frame_images(folder, self.ALLOWED_IMAGE_SUFFIXES)

        if not frames:
            QMessageBox.information(self, "Info", "No image frames found in that folder.")
            return

        self.multi_folder = folder
        self.multi_frame_paths = frames
        self.multi_auto_target_size = None
        self.multi_max_content_size = None
        self.multi_current_original_mem = 0.0
        self.multi_source_cache = {}
        self.multi_prepared_cache = {}
        self.multi_preview_cache = {}
        self.stop_multi_playback()

        self.multi_frame_combo.blockSignals(True)
        self.multi_frame_combo.clear()
        for i, p in enumerate(frames):
            self.multi_frame_combo.addItem(f"Frame {i + 1}: {p.name}", str(p))
        self.multi_frame_combo.setCurrentIndex(0)
        self.multi_frame_combo.blockSignals(False)

        # Pre-process all frames once: fills prepared-cache and computes max content size
        progress = QProgressDialog("Pre-processing frames...", None, 0, len(frames), self)
        progress.setWindowTitle("Multi Frame Loading")
        progress.setWindowModality(Qt.WindowModal)
        progress.setMinimumDuration(0)
        progress.setValue(0)
        QApplication.processEvents()
        max_w, max_h = 0, 0
        try:
            for idx, fp in enumerate(frames, start=1):
                progress.setLabelText(f"Processing {fp.name} ({idx}/{len(frames)})...")
                QApplication.processEvents()
                cropped = self._get_multi_prepared_content(fp)
                max_w = max(max_w, cropped.size[0])
                max_h = max(max_h, cropped.size[1])
                progress.setValue(idx)
                QApplication.processEvents()
        finally:
            progress.close()
        if max_w > 0 and max_h > 0:
            self.multi_max_content_size = (max_w, max_h)

        self.multi_frame_combo.setEnabled(True)
        self.multi_mode_combo.setEnabled(True)
        self.multi_prev_btn.setEnabled(True)
        self.multi_play_btn.setEnabled(True)
        self.multi_stop_btn.setEnabled(True)
        self.multi_next_btn.setEnabled(True)
        self.multi_export_frame_btn.setEnabled(True)
        self.multi_export_all_btn.setEnabled(True)
        self.multi_info_label.setText(f"Loaded {len(frames)} frames from: {folder.name}")
        self.refresh_multi_frame_preview()

    def _get_multi_source_image(self, frame_path):
        key = str(frame_path)
        cached = self.multi_source_cache.get(key)
        if cached is not None:
            return cached.copy()

        img = Image.open(str(frame_path))
        prepared = img.copy()
        self.multi_source_cache[key] = prepared
        return prepared.copy()

    def _get_multi_prepared_content(self, frame_path):
        key = str(frame_path)
        cached = self.multi_prepared_cache.get(key)
        if cached is not None:
            return cached.copy()

        img = Image.open(str(frame_path))
        prepared = prepare_optimized_content(img)
        self.multi_prepared_cache[key] = prepared.copy()
        return prepared.copy()

    def _get_multi_preview_payload(self, frame_path, effective_target, outline_size):
        cache_key = (str(frame_path), effective_target, outline_size)
        cached = self.multi_preview_cache.get(cache_key)
        if cached is not None:
            return cached

        cropped = self._get_multi_prepared_content(frame_path)
        final_img = finalize_prepared_content(cropped.copy(), target=effective_target)
        if effective_target is not None:
            preview_pixmap = pil_to_qpixmap(
                draw_canvas_preview(cropped, effective_target[0], effective_target[1], outline_size=outline_size)
            )
        else:
            preview_pixmap = pil_to_qpixmap(draw_crop_outline(final_img, cropped)).scaled(300, 300, Qt.KeepAspectRatio)

        payload = {
            "cropped_size": cropped.size,
            "final_size": final_img.size,
            "disk_size_est": format_file_size(estimate_png_disk_size_bytes(final_img)),
            "preview_pixmap": preview_pixmap,
        }
        self.multi_preview_cache[cache_key] = payload
        return payload

    def get_multi_effective_target_canvas_size(self):
        target = self.get_multi_target_canvas_size()
        if target is not None:
            return target

        if self.multi_auto_target_size is not None:
            return self.multi_auto_target_size

        if not self.multi_frame_paths:
            return None

        # Frames were pre-processed at load time; use cached max size directly
        if self.multi_max_content_size is not None:
            max_w, max_h = self.multi_max_content_size
            self.multi_auto_target_size = (
                next_multiple_of_4(max_w),
                next_multiple_of_4(max_h),
            )
            return self.multi_auto_target_size

        raise ValueError("Max content size not yet computed. Load a folder first.")

    def _get_multi_outline_size(self):
        return self.multi_max_content_size  # Always pre-computed at load time

    def go_to_prev_multi_frame(self):
        if not self.multi_frame_paths:
            return
        idx = (self.multi_frame_combo.currentIndex() - 1) % len(self.multi_frame_paths)
        self.multi_frame_combo.setCurrentIndex(idx)

    def go_to_next_multi_frame(self):
        if not self.multi_frame_paths:
            return
        idx = (self.multi_frame_combo.currentIndex() + 1) % len(self.multi_frame_paths)
        self.multi_frame_combo.setCurrentIndex(idx)

    def start_multi_playback(self):
        if len(self.multi_frame_paths) < 2:
            return
        self.multi_player_timer.start(self.MULTI_PLAYBACK_INTERVAL_MS)
        self.multi_info_label.setText("Playing frames...")

    def stop_multi_playback(self):
        if self.multi_player_timer.isActive():
            self.multi_player_timer.stop()

    def _advance_multi_frame(self):
        if not self.multi_frame_paths:
            self.stop_multi_playback()
            return
        next_index = (self.multi_frame_combo.currentIndex() + 1) % len(self.multi_frame_paths)
        self.multi_frame_combo.blockSignals(True)
        self.multi_frame_combo.setCurrentIndex(next_index)
        self.multi_frame_combo.blockSignals(False)
        self._refresh_multi_frame_preview(update_original_preview=False)

    def refresh_multi_frame_preview(self):
        self._refresh_multi_frame_preview(update_original_preview=True)

    def _refresh_multi_frame_preview(self, update_original_preview):
        if not self.multi_frame_paths:
            return

        current_path = self.multi_frame_combo.currentData()
        if not current_path:
            return

        try:
            src = Path(current_path)
            requested_target = self.get_multi_target_canvas_size()
            effective_target = self.get_multi_effective_target_canvas_size()
            outline_size = self._get_multi_outline_size()
            preview_payload = self._get_multi_preview_payload(src, effective_target, outline_size)
            cropped_size = preview_payload["cropped_size"]
            fw, fh = preview_payload["final_size"]

            current_idx = self.multi_frame_combo.currentIndex()
            total = len(self.multi_frame_paths)
            frame_name = src.name
            self.multi_frame_counter.setText(f"{frame_name}   {current_idx + 1} / {total}")

            if update_original_preview:
                img = self._get_multi_source_image(src)
                self.multi_original_preview.setPixmap(
                    pil_to_qpixmap(img).scaled(300, 300, Qt.KeepAspectRatio)
                )
                ow, oh = img.size
                orig_mem = estimate_memory_mb(ow, oh)
                self.multi_current_original_mem = orig_mem
                self.multi_original_info.setText(
                    f"Size: {ow}×{oh}\n"
                    f"Raw Memory: {orig_mem:.2f} MB\n"
                    f"Disk Size: {format_file_size(os.path.getsize(str(src)))}"
                )
            else:
                orig_mem = self.multi_current_original_mem

            self.multi_cropped_preview.setPixmap(preview_payload["preview_pixmap"])

            final_mem = estimate_memory_mb(fw, fh)
            saved = orig_mem - final_mem
            saved_pct = (saved / orig_mem) * 100 if orig_mem > 0 else 0

            if requested_target is None:
                self.multi_cropped_info.setText(
                    f"Content: {cropped_size[0]}×{cropped_size[1]}\n"
                    f"Shared: {fw}×{fh}\n"
                    f"Raw Memory: {final_mem:.2f} MB\n"
                    f"Disk Size (PNG est.): {preview_payload['disk_size_est']}\n"
                    f"Saved: {saved:.2f} MB ({saved_pct:.1f}%)"
                )
            else:
                self.multi_cropped_info.setText(
                    f"Content: {cropped_size[0]}×{cropped_size[1]}\n"
                    f"Final: {fw}×{fh}\n"
                    f"Raw Memory: {final_mem:.2f} MB\n"
                    f"Disk Size (PNG est.): {preview_payload['disk_size_est']}\n"
                    f"Saved: {saved:.2f} MB ({saved_pct:.1f}%)"
                )
            if requested_target is None and effective_target is not None:
                self.multi_info_label.setText(
                    f"{src.name} optimized (Auto shared {effective_target[0]}×{effective_target[1]})"
                )
            else:
                self.multi_info_label.setText(
                    f"{src.name} optimized ({self.multi_mode_combo.currentText()})"
                )
        except Exception as e:
            self.multi_cropped_preview.setText("Optimization failed")
            self.multi_cropped_info.setText("Size: -\nRaw Memory: -\nDisk Size: -\nSaved: -")
            self.multi_info_label.setText(f"Failed to preview frame: {e}")

    def export_selected_multi_frame(self):
        if not self.multi_frame_paths:
            QMessageBox.information(self, "Info", "No frames loaded.")
            return

        current_path = self.multi_frame_combo.currentData()
        if not current_path:
            QMessageBox.information(self, "Info", "Select a frame first.")
            return

        src = Path(current_path)
        try:
            cropped = self._get_multi_prepared_content(src)
            out_img = finalize_prepared_content(cropped, target=self.get_multi_effective_target_canvas_size())
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to process frame:\n{e}")
            return

        default_name = f"{src.stem}_optimized{src.suffix}"
        save_path, _ = QFileDialog.getSaveFileName(
            self,
            "Save Optimized Frame",
            str(src.with_name(default_name)),
            "Images (*.png *.jpg *.jpeg *.bmp)"
        )
        if not save_path:
            return

        try:
            out_img.save(save_path)
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to save frame:\n{e}")
            return

        disk_size = format_file_size(os.path.getsize(save_path)) if os.path.exists(save_path) else "Unknown"

        QMessageBox.information(
            self,
            "Saved",
            f"Saved frame to:\n{save_path}\n\nActual File Size: {disk_size}"
        )

    def export_all_multi_frames(self):
        if not self.multi_frame_paths:
            QMessageBox.information(self, "Info", "No frames loaded.")
            return

        out_dir = QFileDialog.getExistingDirectory(self, "Select Output Folder", "")
        if not out_dir:
            return

        out_folder = Path(out_dir)
        progress = QProgressDialog("Exporting frames...", None, 0, len(self.multi_frame_paths), self)
        progress.setWindowTitle("Export All Frames")
        progress.setWindowModality(Qt.WindowModal)
        progress.setMinimumDuration(0)
        progress.setValue(0)
        QApplication.processEvents()

        success = 0
        failed = []
        total_disk_bytes = 0
        for i, src in enumerate(self.multi_frame_paths, start=1):
            progress.setLabelText(f"Processing {src.name} ({i}/{len(self.multi_frame_paths)})...")
            QApplication.processEvents()
            try:
                cropped = self._get_multi_prepared_content(src)
                out_img = finalize_prepared_content(cropped, target=self.get_multi_effective_target_canvas_size())
                out_path = out_folder / f"{src.stem}_optimized{src.suffix}"
                out_img.save(str(out_path))
                if out_path.exists():
                    total_disk_bytes += out_path.stat().st_size
                success += 1
            except Exception as e:
                failed.append(f"{src.name}: {e}")

            progress.setValue(i)
            QApplication.processEvents()

        progress.close()

        if failed:
            preview = "\n".join(failed[:3])
            more = "" if len(failed) <= 3 else f"\n...and {len(failed) - 3} more"
            QMessageBox.warning(
                self,
                "Export Complete (with issues)",
                f"Exported {success}/{len(self.multi_frame_paths)} frames to:\n{out_folder}\n\n"
                f"Actual Total File Size: {format_file_size(total_disk_bytes)}\n\n"
                f"Failed:\n{preview}{more}"
            )
        else:
            QMessageBox.information(
                self,
                "Export Complete",
                f"Exported {success} frames to:\n{out_folder}\n\n"
                f"Actual Total File Size: {format_file_size(total_disk_bytes)}"
            )


if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = TextureOptimizerUI()
    window.resize(900, 500)
    window.show()
    sys.exit(app.exec_())
