import sys
from pathlib import Path

from PySide2.QtWidgets import (
    QApplication, QWidget, QLabel, QPushButton, QVBoxLayout, QHBoxLayout,
    QFileDialog, QMessageBox, QProgressDialog, QComboBox, QSpinBox
)
from PySide2.QtGui import QPixmap, QImage
from PySide2.QtCore import Qt

from PIL import Image, ImageDraw

from image_tools import (
    find_tight_bbox,
    remove_background,
    crop_to_bbox,
    expand_bbox_to_multiple_of_4,
    expand_to_multiple_of_4,
    draw_crop_outline,
    estimate_memory_mb
)


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


def draw_canvas_preview(content_img, canvas_w, canvas_h, preview_size=300):
    """
    Visualize how content sits inside a target canvas.
    Renders at preview_size resolution so it's always fast.
    Shows a checkerboard background for empty space.
    """
    scale = min(preview_size / canvas_w, preview_size / canvas_h, 1.0)
    disp_w = max(1, int(canvas_w * scale))
    disp_h = max(1, int(canvas_h * scale))

    check = 10
    canvas = Image.new("RGBA", (disp_w, disp_h))
    draw = ImageDraw.Draw(canvas)
    for y in range(0, disp_h, check):
        for x in range(0, disp_w, check):
            c = (52, 52, 52, 255) if ((x // check) + (y // check)) % 2 == 0 else (32, 32, 32, 255)
            draw.rectangle(
                [x, y, min(x + check - 1, disp_w - 1), min(y + check - 1, disp_h - 1)],
                fill=c
            )

    cw, ch = content_img.size
    disp_cw = max(1, int(cw * scale))
    disp_ch = max(1, int(ch * scale))
    content_scaled = content_img.resize((disp_cw, disp_ch), Image.NEAREST)

    ox = (disp_w - disp_cw) // 2
    oy = (disp_h - disp_ch) // 2
    canvas.paste(content_scaled, (ox, oy), content_scaled)

    # Canvas border
    draw.rectangle([0, 0, disp_w - 1, disp_h - 1], outline=(80, 80, 80, 255), width=2)
    # Content bounds outline
    draw.rectangle([ox, oy, ox + disp_cw, oy + disp_ch], outline=(0, 255, 0, 255), width=3)

    return canvas


class TextureOptimizerUI(QWidget):
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
        self.setStyleSheet(open("style.qss").read())

        self.original_path = None
        self.original_image = None
        self.cropped_image = None

        self.init_ui()

    def init_ui(self):
        # Original panel
        self.original_title = QLabel("Original Image")
        self.original_title.setObjectName("panelTitle")

        self.original_preview = QLabel("No image loaded")
        self.original_preview.setAlignment(Qt.AlignCenter)
        self.original_preview.setObjectName("imagePreview")

        self.original_info = QLabel("Size: -\nMemory: -")
        self.original_info.setObjectName("infoLabel")

        original_layout = QVBoxLayout()
        original_layout.addWidget(self.original_title)
        original_layout.addWidget(self.original_preview)
        original_layout.addWidget(self.original_info)

        # Optimized panel
        self.cropped_title = QLabel("Optimized Image")
        self.cropped_title.setObjectName("panelTitle")

        # Mode dropdown next to the Optimized title
        self.mode_combo = QComboBox()
        self.mode_combo.addItems(["Auto", "128 × 128", "256 × 256", "512 × 512", "1024 × 1024", "2048 × 2048", "Custom..."])
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

        optimized_header = QHBoxLayout()
        optimized_header.addWidget(self.cropped_title)
        optimized_header.addStretch()
        optimized_header.addWidget(self.mode_combo)

        optimized_header_col = QVBoxLayout()
        optimized_header_col.setSpacing(2)
        optimized_header_col.addLayout(optimized_header)
        optimized_header_col.addWidget(self.custom_row_widget)

        self.cropped_preview = QLabel("No crop yet")
        self.cropped_preview.setAlignment(Qt.AlignCenter)
        self.cropped_preview.setObjectName("imagePreview")

        self.cropped_info = QLabel("Size: -\nMemory: -\nSaved: -")
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

        self.auto_crop_btn = QPushButton("Crop")
        self.auto_crop_btn.clicked.connect(self.handle_auto_crop)
        self.auto_crop_btn.setEnabled(False)

        self.export_btn = QPushButton("Export")
        self.export_btn.clicked.connect(self.handle_export)
        self.export_btn.setObjectName("exportButton")
        self.export_btn.setEnabled(False)

        self.mode_combo.setEnabled(False)
        self.custom_w_spin.setEnabled(False)
        self.custom_h_spin.setEnabled(False)

        button_layout = QHBoxLayout()
        button_layout.addWidget(self.select_btn)
        button_layout.addWidget(self.auto_crop_btn)
        button_layout.addWidget(self.export_btn)

        # Main layout
        images_layout = QHBoxLayout()
        images_layout.addLayout(original_layout)
        images_layout.addLayout(cropped_layout)

        main_layout = QVBoxLayout()
        main_layout.addLayout(images_layout)
        main_layout.addWidget(self.summary_label)
        main_layout.addLayout(button_layout)

        self.setLayout(main_layout)

    # ---------------------------
    # Mode dropdown helpers
    # ---------------------------
    def _on_mode_changed(self, text):
        self.custom_row_widget.setVisible(text == "Custom...")
        self.custom_w_spin.setEnabled(text == "Custom..." and self.original_image is not None)
        self.custom_h_spin.setEnabled(text == "Custom..." and self.original_image is not None)
        self.custom_apply_btn.setEnabled(text == "Custom..." and self.original_image is not None)
        if text == "Custom..." and self.original_image is not None:
            self.summary_label.setText("Set custom size and press Update.")
        if self.original_image is not None and text != "Custom...":
            self.handle_auto_crop()

    def _apply_custom_size(self):
        if self.mode_combo.currentText() == "Custom..." and self.original_image is not None:
            self.handle_auto_crop()

    def get_target_canvas_size(self):
        """Returns (width, height) or None for Auto (tight crop)."""
        text = self.mode_combo.currentText()
        if text == "Auto":
            return None
        if text == "Custom...":
            return (self.custom_w_spin.value(), self.custom_h_spin.value())
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
        self.auto_crop_btn.setEnabled(True)
        self.mode_combo.setEnabled(True)
        self.custom_w_spin.setEnabled(self.mode_combo.currentText() == "Custom...")
        self.custom_h_spin.setEnabled(self.mode_combo.currentText() == "Custom...")
        self.custom_apply_btn.setEnabled(self.mode_combo.currentText() == "Custom...")
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
            f"Memory: {mem:.2f} MB\n"
            f"Empty: {empty_pct:.1f}%\n"
            f"{status}"
        )

        self.cropped_preview.setText("No crop yet")
        self.cropped_info.setText("Size: -\nMemory: -\nSaved: -")
        if self.mode_combo.currentText() == "Custom...":
            self.summary_label.setText("Image loaded. Set custom size and press Update.")
        else:
            self.summary_label.setText("Image loaded. Running Auto crop...")
        QApplication.processEvents()

        # Auto-run crop immediately on load, except Custom mode which waits for Update.
        if self.mode_combo.currentText() != "Custom...":
            self.handle_auto_crop()

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
                self.cropped_info.setText(
                    f"Final: {fw}×{fh}\n"
                    f"Memory: {final_mem:.2f} MB\n"
                    f"Saved: {saved:.2f} MB ({saved_pct:.1f}%)"
                )
            else:
                self.cropped_info.setText(
                    f"Content: {cropped.size[0]}×{cropped.size[1]}\n"
                    f"Final: {fw}×{fh}\n"
                    f"Memory: {final_mem:.2f} MB\n"
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

        QMessageBox.information(self, "Saved", f"Saved to:\n{save_path}")


if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = TextureOptimizerUI()
    window.resize(900, 500)
    window.show()
    sys.exit(app.exec_())
