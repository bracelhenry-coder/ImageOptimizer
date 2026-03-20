from collections import deque

from PIL import Image, ImageChops, ImageDraw


def find_tight_bbox(img: Image.Image, tolerance=30):
    """
    Detect tight bounding box of non-background pixels.
    Background = top-left pixel, with tolerance.
    Works even with noisy white backgrounds.
    """
    if img.mode != "RGBA":
        img = img.convert("RGBA")

    bg_color = img.getpixel((0, 0))[:3]  # ignore alpha
    bg_img = Image.new("RGB", img.size, bg_color)

    diff = ImageChops.difference(img.convert("RGB"), bg_img)
    diff = diff.convert("L")

    # Apply tolerance threshold
    diff = diff.point(lambda p: 255 if p > tolerance else 0)

    return diff.getbbox()


def remove_background(img: Image.Image, tolerance=12, progress_callback=None):
    """
    Remove background pixels using BFS flood-fill from all image edges.
    Detects background color from the top-left pixel with tolerance.
    Makes all connected background pixels fully transparent.
    """
    if img.mode != "RGBA":
        img = img.convert("RGBA")

    img = img.copy()
    w, h = img.size
    data = img.load()
    bg_r, bg_g, bg_b = data[0, 0][:3]
    total_pixels = w * h

    # Build a flat mask of candidate background pixels.
    bg_mask = bytearray(total_pixels)
    index = 0
    candidate_count = 0
    row_update_step = max(1, h // 80)

    if progress_callback:
        progress_callback(0)

    for y in range(h):
        for x in range(w):
            r, g, b, _ = data[x, y]
            if (
                abs(r - bg_r) <= tolerance and
                abs(g - bg_g) <= tolerance and
                abs(b - bg_b) <= tolerance
            ):
                bg_mask[index] = 1
                candidate_count += 1
            index += 1

        if progress_callback and (y % row_update_step == 0 or y == h - 1):
            # 0..60% reserved for candidate mask build.
            progress_callback(int(((y + 1) / h) * 60))

    queue = deque()

    def seed(x, y):
        idx = y * w + x
        if bg_mask[idx]:
            bg_mask[idx] = 0
            queue.append(idx)

    for x in range(w):
        seed(x, 0)
        if h > 1:
            seed(x, h - 1)

    for y in range(1, h - 1):
        seed(0, y)
        if w > 1:
            seed(w - 1, y)

    processed = 0
    flood_update_step = max(1024, total_pixels // 400)

    while queue:
        idx = queue.popleft()
        processed += 1

        if progress_callback and (processed % flood_update_step == 0):
            # 60..100% reserved for flood fill.
            if candidate_count > 0:
                flood_pct = int((processed / candidate_count) * 40)
            else:
                flood_pct = 40
            progress_callback(min(100, 60 + flood_pct))

        x = idx % w
        y = idx // w
        data[x, y] = (0, 0, 0, 0)

        if x > 0:
            left_idx = idx - 1
            if bg_mask[left_idx]:
                bg_mask[left_idx] = 0
                queue.append(left_idx)

        if x + 1 < w:
            right_idx = idx + 1
            if bg_mask[right_idx]:
                bg_mask[right_idx] = 0
                queue.append(right_idx)

        if y > 0:
            up_idx = idx - w
            if bg_mask[up_idx]:
                bg_mask[up_idx] = 0
                queue.append(up_idx)

        if y + 1 < h:
            down_idx = idx + w
            if bg_mask[down_idx]:
                bg_mask[down_idx] = 0
                queue.append(down_idx)

    if progress_callback:
        progress_callback(100)

    return img


def crop_to_bbox(img: Image.Image, bbox):
    """Crop image to bounding box."""
    if bbox is None:
        return img
    return img.crop(bbox)


def expand_bbox_to_multiple_of_4(bbox, image_size):
    """
    Expand a bbox outward so its width and height are multiples of 4.
    This keeps original pixels unchanged and avoids any resampling.
    If the image edge prevents a perfect fit, a later transparent pad can
    still be applied as a fallback.
    """
    if bbox is None:
        return None

    image_w, image_h = image_size
    left, top, right, bottom = bbox

    def next_mult4(n):
        return n if n % 4 == 0 else n + (4 - n % 4)

    def expand_range(start, end, limit):
        size = end - start
        target = next_mult4(size)
        extra = target - size

        start -= extra // 2
        end += extra - (extra // 2)

        if start < 0:
            end = min(limit, end - start)
            start = 0

        if end > limit:
            start = max(0, start - (end - limit))
            end = limit

        return start, end

    left, right = expand_range(left, right, image_w)
    top, bottom = expand_range(top, bottom, image_h)

    return left, top, right, bottom


def expand_to_multiple_of_4(img: Image.Image):
    """
    Pad image so both width and height are multiples of 4.
    Required for Unity texture compression (DXT/BC formats use 4x4 pixel blocks).
    Content is centered on the canvas. No forced square — saves memory.
    """
    w, h = img.size

    def next_mult4(n):
        return n if n % 4 == 0 else n + (4 - n % 4)

    new_w = next_mult4(w)
    new_h = next_mult4(h)

    canvas = Image.new("RGBA", (new_w, new_h), (0, 0, 0, 0))

    offset_x = (new_w - w) // 2
    offset_y = (new_h - h) // 2
    canvas.paste(img, (offset_x, offset_y))

    return canvas


def draw_crop_outline(final_img: Image.Image, cropped_img: Image.Image):
    """
    Draw a green outline around the cropped region
    inside the final square canvas.
    """
    outlined = final_img.copy()
    draw = ImageDraw.Draw(outlined)

    fw, fh = final_img.size
    cw, ch = cropped_img.size

    offset_x = (fw - cw) // 2
    offset_y = (fh - ch) // 2

    draw.rectangle(
        [offset_x, offset_y, offset_x + cw, offset_y + ch],
        outline=(0, 255, 0, 255),
        width=4
    )

    return outlined


def estimate_memory_mb(w, h):
    return (w * h * 4) / (1024 * 1024)
