"""Local before/after screenshot verifier (handover for Phase 6).

verify(before, after) compares two in-memory PIL images and returns
{changed, distance, diff_ratio, summary}. Nothing is read from or written to disk.
"""
from PIL import Image, ImageChops

HASH_SIZE = 8          # dHash grid: 8 rows x 8 neighbour comparisons = 64 bits
PIXEL_TOLERANCE = 16   # per-channel difference (0-255) that is ignored as noise
DIFF_MAX_SIDE = 256    # pixel diff runs at most at this size, keeping the aspect ratio of before
CHANGED_DISTANCE = 6   # Hamming distance (bits of 64) at or above which the screen counts as changed
CHANGED_RATIO = 0.01   # share of differing pixels at or above which the screen counts as changed


def _check(image, name):
    if not isinstance(image, Image.Image):
        raise TypeError(f'{name} must be a PIL.Image.Image, got {type(image).__name__}')
    if image.width < 1 or image.height < 1:
        raise ValueError(f'{name} is empty ({image.width}x{image.height})')
    return image


def to_rgb(image):
    """Flatten any mode (RGBA, P, L, ...) onto white and return an RGB image."""
    try:
        rgba = image.convert('RGBA')
    except (ValueError, OSError) as error:
        raise ValueError(f'unsupported image mode {image.mode}') from error
    background = Image.new('RGBA', rgba.size, (255, 255, 255, 255))
    return Image.alpha_composite(background, rgba).convert('RGB')


def dhash(image):
    """64-bit difference hash: each bit says whether a cell is brighter than its right neighbour."""
    width = HASH_SIZE + 1
    gray = to_rgb(_check(image, 'image')).convert('L').resize((width, HASH_SIZE), Image.Resampling.BOX)
    pixels = gray.tobytes()
    bits = 0
    for row in range(HASH_SIZE):
        for col in range(HASH_SIZE):
            left, right = pixels[row * width + col], pixels[row * width + col + 1]
            bits = (bits << 1) | int(left > right)
    return bits


def hamming(first, second):
    return bin(first ^ second).count('1')


def _diff_size(size):
    width, height = size
    scale = min(1.0, DIFF_MAX_SIDE / max(width, height))
    return max(1, round(width * scale)), max(1, round(height * scale))


def pixel_diff_ratio(before, after, tolerance=PIXEL_TOLERANCE):
    """Share of pixels whose largest channel difference exceeds tolerance (after is scaled to before)."""
    size = _diff_size(_check(before, 'before').size)
    first, second = to_rgb(before), to_rgb(_check(after, 'after'))
    if first.size != size:
        first = first.resize(size, Image.Resampling.BOX)
    if second.size != size:
        second = second.resize(size, Image.Resampling.BOX)
    red, green, blue = ImageChops.difference(first, second).split()
    peak = ImageChops.lighter(ImageChops.lighter(red, green), blue)
    over = peak.point(lambda value: 255 if value > tolerance else 0)
    return over.histogram()[255] / (size[0] * size[1])


def verify(before, after):
    """Compare before/after screenshots; changed is True when either limit is reached."""
    _check(before, 'before')
    _check(after, 'after')
    distance = hamming(dhash(before), dhash(after))
    ratio = round(pixel_diff_ratio(before, after), 6)
    changed = distance >= CHANGED_DISTANCE or ratio >= CHANGED_RATIO
    sizes = f'{before.width}x{before.height} -> {after.width}x{after.height}'
    summary = (f"{'Changed' if changed else 'No visible change'}: dHash distance {distance}/64 "
               f'(limit {CHANGED_DISTANCE}), {ratio:.2%} of pixels differ (limit {CHANGED_RATIO:.0%}), size {sizes}')
    return dict(changed=changed, distance=distance, diff_ratio=ratio, summary=summary)
