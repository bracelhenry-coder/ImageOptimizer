from pathlib import Path

from PIL import Image, ImageDraw

from image_tools import (
    crop_to_bbox,
    draw_crop_outline,
    expand_bbox_to_multiple_of_4,
    expand_to_multiple_of_4,
    remove_background,
)


def is_supported_image_path(path, allowed_suffixes):
    return path.is_file() and path.suffix.lower() in allowed_suffixes


def is_supported_frames_folder(path, allowed_suffixes):
    if not path.is_dir():
        return False
    for p in path.iterdir():
        if p.is_file() and p.suffix.lower() in allowed_suffixes:
            return True
    return False


def list_frame_images(folder, allowed_suffixes):
    folder = Path(folder)
    return sorted(
        [p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in allowed_suffixes],
        key=lambda p: p.name.lower(),
    )


def next_multiple_of_4(value):
    return value if value % 4 == 0 else value + (4 - value % 4)


def prepare_optimized_content(source_img):
    img = source_img.convert("RGBA")
    img = remove_background(img)

    bbox = img.split()[3].getbbox()
    if bbox is None:
        raise ValueError("No visible pixels found after background removal.")

    bbox = expand_bbox_to_multiple_of_4(bbox, img.size)
    return crop_to_bbox(img, bbox)


def finalize_prepared_content(cropped, target=None):
    if target is not None:
        tw, th = target
        cw, ch = cropped.size
        if cw > tw or ch > th:
            raise ValueError(
                f"Cropped content ({cw}x{ch}) is larger than target ({tw}x{th})."
            )
        canvas = Image.new("RGBA", (tw, th), (0, 0, 0, 0))
        offset_x = (tw - cw) // 2
        offset_y = (th - ch) // 2
        canvas.paste(cropped, (offset_x, offset_y))
        return canvas

    return expand_to_multiple_of_4(cropped)


def optimize_image_for_export(source_img, target=None):
    cropped = prepare_optimized_content(source_img)
    return finalize_prepared_content(cropped, target)


def draw_canvas_preview(content_img, canvas_w, canvas_h, preview_size=300, outline_size=None):
    """Render a lightweight canvas preview with checkerboard and green outline."""
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
                fill=c,
            )

    cw, ch = content_img.size
    disp_cw = max(1, int(cw * scale))
    disp_ch = max(1, int(ch * scale))
    content_scaled = content_img.resize((disp_cw, disp_ch), Image.NEAREST)

    ox = (disp_w - disp_cw) // 2
    oy = (disp_h - disp_ch) // 2
    canvas.paste(content_scaled, (ox, oy), content_scaled)

    draw.rectangle([0, 0, disp_w - 1, disp_h - 1], outline=(80, 80, 80, 255), width=2)
    outline_w, outline_h = outline_size or content_img.size
    disp_outline_w = max(1, int(outline_w * scale))
    disp_outline_h = max(1, int(outline_h * scale))
    outline_x = (disp_w - disp_outline_w) // 2
    outline_y = (disp_h - disp_outline_h) // 2
    draw.rectangle(
        [outline_x, outline_y, outline_x + disp_outline_w, outline_y + disp_outline_h],
        outline=(0, 255, 0, 255),
        width=3,
    )

    return canvas


def optimize_image_for_preview(source_img, target=None):
    cropped = prepare_optimized_content(source_img)

    if target is not None:
        tw, th = target
        final = finalize_prepared_content(cropped, target)
        return final, draw_canvas_preview(cropped, tw, th), cropped.size

    final = expand_to_multiple_of_4(cropped)
    return final, draw_crop_outline(final, cropped), cropped.size
