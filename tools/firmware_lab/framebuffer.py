"""Pure synthetic display-access models, with no firmware or device operations.

``plan_reference_blit`` defines a safe host-side rectangle contract. In contrast,
``plan_observed_blit`` traces a bounded hypothesis from static observations:
clipped inclusive ends and groups of four source pixels. Its extents are *not*
ordinary width/height counts, and the original caller's padding convention is
unresolved. Neither function is a native ABI implementation or a display driver.

Both return byte-offset spans instead of allocating or accessing pixel buffers.
The 320 x 240 coordinate grid and a 640 x 480 RGB565 internal surface are separate
quantities. The internal allocation does not establish physical panel resolution.
"""

from __future__ import annotations

LOGICAL_WIDTH = 320
LOGICAL_HEIGHT = 240
INTERNAL_WIDTH = 640
INTERNAL_HEIGHT = 480
INTERNAL_STRIDE_BYTES = INTERNAL_WIDTH * 2
INTERNAL_BUFFER_BYTES = INTERNAL_STRIDE_BYTES * INTERNAL_HEIGHT
MAX_DIMENSION = 1024
MAX_STRIDE_BYTES = 64 * 1024
MAX_BUFFER_BYTES = 16 * 1024 * 1024
UINT32_MAX = (1 << 32) - 1


class FramebufferModelError(ValueError):
    """A synthetic request has invalid or insufficiently bounded storage."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise FramebufferModelError(message)


def _uint(value: int, name: str, maximum: int = UINT32_MAX) -> None:
    _require(type(value) is int and 0 <= value <= maximum,
             name + " must be an integer in 0.." + str(maximum))


def argb8888_to_rgb565(word: int) -> int:
    """Convert numeric 0xAARRGGBB to RGB565; alpha has no effect.

    A big-endian source word has byte order A, R, G, B. This function accepts a
    number, not host-endian bytes, and does no alpha blending or color correction.
    """
    _uint(word, "word")
    return ((word >> 8) & 0xF800) | ((word >> 5) & 0x07E0) | ((word >> 3) & 0x001F)


def _source_layout(*, source_width: int, source_height: int,
                   source_stride_bytes: int, source_buffer_bytes: int,
                   source_offset_bytes: int, source_padding_pixels: int,
                   source_padding_rows: int) -> dict:
    for name, value in (("source_width", source_width),
                        ("source_height", source_height),
                        ("source_padding_pixels", source_padding_pixels),
                        ("source_padding_rows", source_padding_rows)):
        _uint(value, name, MAX_DIMENSION)
    _require(source_width > 0 and source_height > 0,
             "active source dimensions must be positive")
    storage_width = source_width + source_padding_pixels
    storage_height = source_height + source_padding_rows
    _require(storage_width <= MAX_DIMENSION and storage_height <= MAX_DIMENSION,
             "padded source dimensions exceed model limit")
    _uint(source_stride_bytes, "source_stride_bytes", MAX_STRIDE_BYTES)
    _require(source_stride_bytes % 4 == 0 and source_stride_bytes >= storage_width * 4,
             "source stride must be word-aligned and cover active pixels plus declared padding")
    _uint(source_buffer_bytes, "source_buffer_bytes", MAX_BUFFER_BYTES)
    _uint(source_offset_bytes, "source_offset_bytes", MAX_BUFFER_BYTES)
    _require(source_offset_bytes % 4 == 0, "source offset must be word-aligned")
    required_bytes = source_offset_bytes + source_stride_bytes * storage_height
    _require(required_bytes <= source_buffer_bytes,
             "source buffer must cover every declared storage row including stride padding")
    return {
        "width": source_width, "height": source_height,
        "padding_pixels": source_padding_pixels, "padding_rows": source_padding_rows,
        "storage_width": storage_width, "storage_height": storage_height,
        "stride_bytes": source_stride_bytes, "buffer_bytes": source_buffer_bytes,
        "offset_bytes": source_offset_bytes, "required_bytes": required_bytes,
        "format": "numeric-ARGB8888", "source_word_bytes": 4,
    }


def _trace(*, mode: str, request: dict, left: int, top: int, right: int,
           bottom: int, source: dict, allow_declared_padding: bool) -> dict:
    """All rectangle endpoints are half-open here, regardless of public mode."""
    _require(0 <= left < right <= LOGICAL_WIDTH and 0 <= top < bottom <= LOGICAL_HEIGHT,
             "touched rectangle is outside the logical grid")
    if allow_declared_padding:
        _require(right <= source["storage_width"] and bottom <= source["storage_height"],
                 "observed access requires more explicitly declared source padding")
    else:
        _require(right <= source["width"] and bottom <= source["height"],
                 "reference rectangle must remain within active source pixels")
    rows = []
    padding_read_pixels = 0
    row_bytes = (right - left) * 4
    for y in range(top, bottom):
        source_start = source["offset_bytes"] + y * source["stride_bytes"] + left * 4
        source_end = source_start + row_bytes
        # Keep independent capacity checks beside the generated spans. No stride
        # slack becomes readable pixel padding unless the caller declared it.
        row_limit = source["offset_bytes"] + y * source["stride_bytes"] + source["storage_width"] * 4
        _require(source_end <= row_limit and source_end <= source["buffer_bytes"],
                 "source span exceeds its declared row or buffer")
        first_start = (y * 2) * INTERNAL_STRIDE_BYTES + left * 4
        second_start = first_start + INTERNAL_STRIDE_BYTES
        spans = [[first_start, first_start + row_bytes],
                 [second_start, second_start + row_bytes]]
        _require(spans[1][1] <= INTERNAL_BUFFER_BYTES,
                 "destination span exceeds internal surface")
        rows.append({"logical_y": y, "source_start": source_start,
                     "source_end_exclusive": source_end, "destination_spans": spans})
        padding_read_pixels += ((right - left) if y >= source["height"]
                                else max(0, right - max(left, source["width"])))
    pixels = (right - left) * (bottom - top)
    observed = mode == "observed-access-hypothesis"
    return {
        "format_version": 1, "mode": mode, "request": request,
        "logical_size": [LOGICAL_WIDTH, LOGICAL_HEIGHT],
        "internal_size": [INTERNAL_WIDTH, INTERNAL_HEIGHT],
        "internal_stride_bytes": INTERNAL_STRIDE_BYTES,
        "internal_buffer_bytes": INTERNAL_BUFFER_BYTES,
        "destination_offset_origin": "pixel-base, excluding allocation prefix",
        "destination_format": "RGB565", "alpha_ignored": True,
        "expansion": [2, 2], "source_layout": source,
        "touched_logical_rect": {"x": left, "y": top,
                                 "width": right - left, "height": bottom - top},
        "source_pixels_read": pixels, "padding_read_pixels": padding_read_pixels,
        "source_read_bytes": pixels * 4, "destination_write_bytes": pixels * 8,
        "rows": rows, "abi_verified": False, "hardware_tested": False,
        "caller_padding_contract_resolved": False,
        "interpretation": (
            "Bounded observed-access hypothesis; inclusive extents and whole groups of four. "
            "Explicit storage declarations establish host-model bounds only; native caller "
            "padding, concurrency and device behavior remain unresolved."
            if observed else
            "Safe host reference contract using positive width/height counts; no clipping "
            "or padding reads. This is not the native request ABI."
        ),
    }


def plan_reference_blit(*, x: int, y: int, width: int, height: int,
                        source_width: int, source_height: int,
                        source_stride_bytes: int, source_buffer_bytes: int,
                        source_offset_bytes: int = 0, source_padding_pixels: int = 0,
                        source_padding_rows: int = 0) -> dict:
    """Plan a strict count-based rectangle at the same source/destination x, y.

    The request must fit the active source and logical grid. It is never clipped,
    and declared padding is never read. Each source pixel maps to two adjacent
    RGB565 pixels on each of two internal rows. Spans are [start, end-exclusive].
    Source rows include the full declared byte stride in their capacity check.
    """
    for name, value in (("x", x), ("y", y), ("width", width), ("height", height)):
        _uint(value, name)
    _require(width > 0 and height > 0, "reference width and height must be positive")
    _require(x + width <= LOGICAL_WIDTH and y + height <= LOGICAL_HEIGHT,
             "reference rectangle must fit the logical grid without clipping")
    source = _source_layout(
        source_width=source_width, source_height=source_height,
        source_stride_bytes=source_stride_bytes, source_buffer_bytes=source_buffer_bytes,
        source_offset_bytes=source_offset_bytes, source_padding_pixels=source_padding_pixels,
        source_padding_rows=source_padding_rows)
    return _trace(mode="safe-reference", request={"x": x, "y": y, "width": width, "height": height},
                  left=x, top=y, right=x + width, bottom=y + height,
                  source=source, allow_declared_padding=False)


def plan_observed_blit(*, x: int, y: int, x_extent: int, y_extent: int,
                       source_width: int, source_height: int,
                       source_stride_bytes: int, source_buffer_bytes: int,
                       source_offset_bytes: int = 0, source_padding_pixels: int = 0,
                       source_padding_rows: int = 0) -> dict:
    """Trace inclusive/clipped/group-of-four accesses, not a callable native ABI.

    Origins must be inside the logical grid. Unsigned origin+extent overflow is
    refused before clipping. A zero extent can still touch a row or four-pixel
    group. X starts at its preceding multiple of four; the final group starts at
    the multiple of four containing the clipped inclusive end. Thus extra source
    pixels/rows can be touched. Active dimensions and explicit padding declarations
    must cover them; byte stride slack alone does not authorize extra reads.

    This deliberately does not emulate invalid pointers, wrapped address math,
    hardware faults, masked blits, driver return codes or concurrency.
    """
    for name, value in (("x", x), ("y", y), ("x_extent", x_extent), ("y_extent", y_extent)):
        _uint(value, name)
    _require(x < LOGICAL_WIDTH and y < LOGICAL_HEIGHT,
             "observed origin must be inside the logical grid")
    _require(x + x_extent <= UINT32_MAX and y + y_extent <= UINT32_MAX,
             "unsigned origin plus extent overflows")
    right_inclusive = min(x + x_extent, LOGICAL_WIDTH - 1)
    bottom_inclusive = min(y + y_extent, LOGICAL_HEIGHT - 1)
    left = (x // 4) * 4
    right = ((right_inclusive // 4) + 1) * 4
    source = _source_layout(
        source_width=source_width, source_height=source_height,
        source_stride_bytes=source_stride_bytes, source_buffer_bytes=source_buffer_bytes,
        source_offset_bytes=source_offset_bytes, source_padding_pixels=source_padding_pixels,
        source_padding_rows=source_padding_rows)
    return _trace(mode="observed-access-hypothesis",
                  request={"x": x, "y": y, "x_extent": x_extent, "y_extent": y_extent},
                  left=left, top=y, right=right, bottom=bottom_inclusive + 1,
                  source=source, allow_declared_padding=True)
