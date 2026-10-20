"""Synthetic arithmetic tests; these do not establish a native ABI or device safety."""

import json
import unittest

from tools.firmware_lab.framebuffer import (
    FramebufferModelError,
    argb8888_to_rgb565,
    plan_observed_blit,
    plan_reference_blit,
)


FULL_SOURCE = {
    "source_width": 320,
    "source_height": 240,
    "source_stride_bytes": 1280,
    "source_buffer_bytes": 307200,
}


def reference(**changes):
    arguments = dict(FULL_SOURCE, x=0, y=0, width=1, height=1)
    arguments.update(changes)
    return plan_reference_blit(**arguments)


def observed(**changes):
    arguments = dict(FULL_SOURCE, x=0, y=0, x_extent=0, y_extent=0)
    arguments.update(changes)
    return plan_observed_blit(**arguments)


def materialize_synthetic_trace(plan, source, fill=0xA5):
    """Exercise returned spans using test-only big-endian numeric word storage.

    Byte order here is an explicit synthetic fixture choice, not a claim about
    the phone ABI. The production model never reads or allocates these buffers.
    """
    destination = bytearray([fill]) * 614400
    for row in plan["rows"]:
        start, end = row["source_start"], row["source_end_exclusive"]
        expanded = bytearray()
        for offset in range(start, end, 4):
            word = int.from_bytes(source[offset:offset + 4], "big")
            pixel = argb8888_to_rgb565(word).to_bytes(2, "big")
            expanded.extend(pixel + pixel)
        for start, end in row["destination_spans"]:
            if end - start != len(expanded):
                raise AssertionError("source and destination span lengths differ")
            destination[start:end] = expanded
    return destination


class NumericColorTests(unittest.TestCase):
    def test_primary_and_mixed_colors_have_independent_expected_values(self):
        examples = {
            0xFF000000: 0x0000,
            0xFFFF0000: 0xF800,
            0xFF00FF00: 0x07E0,
            0xFF0000FF: 0x001F,
            0xFFFFFFFF: 0xFFFF,
            0xFFFFFF00: 0xFFE0,
            0xFF00FFFF: 0x07FF,
            0xFFFF00FF: 0xF81F,
            0x12345678: 0x32AF,
            0xABCDEF12: 0xCF62,
        }
        for word, expected in examples.items():
            with self.subTest(word=hex(word)):
                self.assertEqual(argb8888_to_rgb565(word), expected)

    def test_alpha_is_ignored_rather_than_blended(self):
        for alpha in (0, 1, 127, 255):
            with self.subTest(alpha=alpha):
                self.assertEqual(argb8888_to_rgb565((alpha << 24) | 0xFF0000), 0xF800)

    def test_low_color_bits_are_truncated(self):
        self.assertEqual(argb8888_to_rgb565(0x00070307), 0)
        self.assertEqual(argb8888_to_rgb565(0x00080408), 0x0821)

    def test_non_uint32_values_are_rejected(self):
        for word in (True, False, -1, 2 ** 32, 1.0, "0xFFFF0000", None):
            with self.subTest(word=word):
                with self.assertRaises(FramebufferModelError):
                    argb8888_to_rgb565(word)


class ReferenceTraceTests(unittest.TestCase):
    def assert_no_cross_row_or_allocation_writes(self, plan):
        previous_end = 0
        for row in plan["rows"]:
            for start, end in row["destination_spans"]:
                self.assertLess(start, end)
                self.assertGreaterEqual(start, previous_end)
                self.assertEqual(start // 1280, (end - 1) // 1280)
                self.assertLessEqual(end, 614400)
                previous_end = end

    def test_full_logical_frame_covers_internal_surface_exactly(self):
        plan = reference(width=320, height=240)
        self.assertEqual(plan["logical_size"], [320, 240])
        self.assertEqual(plan["internal_size"], [640, 480])
        self.assertEqual(plan["internal_stride_bytes"], 1280)
        self.assertEqual(plan["internal_buffer_bytes"], 614400)
        self.assertEqual(plan["source_pixels_read"], 76800)
        self.assertEqual(plan["source_read_bytes"], 307200)
        self.assertEqual(plan["destination_write_bytes"], 614400)
        self.assertEqual(len(plan["rows"]), 240)
        self.assertEqual(plan["rows"][0], {
            "logical_y": 0, "source_start": 0, "source_end_exclusive": 1280,
            "destination_spans": [[0, 1280], [1280, 2560]],
        })
        self.assertEqual(plan["rows"][-1], {
            "logical_y": 239, "source_start": 305920, "source_end_exclusive": 307200,
            "destination_spans": [[611840, 613120], [613120, 614400]],
        })
        spans = [span for row in plan["rows"] for span in row["destination_spans"]]
        self.assertTrue(all(left[1] == right[0] for left, right in zip(spans, spans[1:])))
        self.assert_no_cross_row_or_allocation_writes(plan)

    def test_bottom_right_pixel_ends_at_last_allocation_byte(self):
        plan = reference(x=319, y=239)
        self.assertEqual(plan["rows"], [{
            "logical_y": 239, "source_start": 307196, "source_end_exclusive": 307200,
            "destination_spans": [[613116, 613120], [614396, 614400]],
        }])
        self.assertEqual(plan["destination_write_bytes"], 8)
        self.assert_no_cross_row_or_allocation_writes(plan)

    def test_offset_stride_and_two_by_two_pixels_preserve_all_sentinels(self):
        plan = reference(x=1, y=2, width=2, height=2,
                         source_width=4, source_height=5, source_stride_bytes=24,
                         source_offset_bytes=8, source_buffer_bytes=128)
        self.assertEqual(plan["rows"], [
            {"logical_y": 2, "source_start": 60, "source_end_exclusive": 68,
             "destination_spans": [[5124, 5132], [6404, 6412]]},
            {"logical_y": 3, "source_start": 84, "source_end_exclusive": 92,
             "destination_spans": [[7684, 7692], [8964, 8972]]},
        ])
        source = bytearray([0xCC]) * 128
        for offset, word in ((60, 0x00FF0000), (64, 0xFF00FF00),
                             (84, 0xFF0000FF), (88, 0xFFFFFFFF)):
            source[offset:offset + 4] = word.to_bytes(4, "big")
        unchanged_source = bytes(source)
        actual = materialize_synthetic_trace(plan, source)
        expected = bytearray([0xA5]) * 614400
        for offset in (5124, 6404):
            expected[offset:offset + 8] = bytes.fromhex("f800f80007e007e0")
        for offset in (7684, 8964):
            expected[offset:offset + 8] = bytes.fromhex("001f001fffffffff")
        self.assertEqual(actual, expected)
        self.assertEqual(bytes(source), unchanged_source)
        self.assertEqual(plan["source_read_bytes"], 16)
        self.assertEqual(plan["destination_write_bytes"], 32)
        self.assertEqual(plan["padding_read_pixels"], 0)
        self.assert_no_cross_row_or_allocation_writes(plan)

    def test_source_stride_changes_only_source_row_addresses(self):
        tight = reference(x=3, y=4, width=2, height=2)
        padded = reference(x=3, y=4, width=2, height=2,
                           source_stride_bytes=1320, source_buffer_bytes=316800)
        self.assertEqual([row["source_start"] for row in tight["rows"]], [5132, 6412])
        self.assertEqual([row["source_start"] for row in padded["rows"]], [5292, 6612])
        self.assertEqual([row["destination_spans"] for row in tight["rows"]],
                         [row["destination_spans"] for row in padded["rows"]])

    def test_reference_rejects_zero_negative_or_outside_rectangles(self):
        for changes in ({"width": 0}, {"height": 0}, {"x": -1}, {"y": -1},
                        {"x": 320}, {"y": 240}, {"x": 319, "width": 2},
                        {"y": 239, "height": 2}, {"width": 321}, {"height": 241}):
            with self.subTest(changes=changes):
                with self.assertRaises(FramebufferModelError):
                    reference(**changes)

    def test_reference_cannot_read_explicitly_declared_padding(self):
        for changes in ({"width": 2}, {"height": 2}):
            with self.subTest(changes=changes):
                with self.assertRaises(FramebufferModelError):
                    reference(source_width=1, source_height=1, source_padding_pixels=3,
                              source_padding_rows=1, source_stride_bytes=16,
                              source_buffer_bytes=32, **changes)

    def test_count_contract_does_not_expand_to_groups_of_four(self):
        plan = reference(x=3, y=5, width=2, height=1)
        self.assertEqual(plan["touched_logical_rect"], {"x": 3, "y": 5, "width": 2, "height": 1})
        self.assertEqual(plan["source_pixels_read"], 2)


class ObservedHypothesisTests(unittest.TestCase):
    def test_zero_extent_is_one_inclusive_row_and_one_group(self):
        plan = observed()
        self.assertEqual(plan["mode"], "observed-access-hypothesis")
        self.assertEqual(plan["touched_logical_rect"], {"x": 0, "y": 0, "width": 4, "height": 1})
        self.assertEqual(plan["source_pixels_read"], 4)
        self.assertEqual(plan["destination_write_bytes"], 32)

    def test_inclusive_delta_four_touches_eight_pixels(self):
        plan = observed(x_extent=4)
        self.assertEqual(plan["touched_logical_rect"], {"x": 0, "y": 0, "width": 8, "height": 1})
        self.assertEqual(plan["rows"][0]["source_end_exclusive"], 32)

    def test_unaligned_origin_rounds_left_and_end_group_outward(self):
        within = observed(x=3, y=2)
        across = observed(x=3, y=2, x_extent=1)
        self.assertEqual(within["touched_logical_rect"], {"x": 0, "y": 2, "width": 4, "height": 1})
        self.assertEqual(across["touched_logical_rect"], {"x": 0, "y": 2, "width": 8, "height": 1})
        self.assertEqual(across["rows"][0]["source_start"], 2560)

    def test_clipped_corner_reads_only_source_pixels_316_through_319(self):
        plan = observed(x=319, y=239, x_extent=100, y_extent=100)
        self.assertEqual(plan["touched_logical_rect"], {"x": 316, "y": 239, "width": 4, "height": 1})
        self.assertEqual(plan["rows"], [{
            "logical_y": 239, "source_start": 307184, "source_end_exclusive": 307200,
            "destination_spans": [[613104, 613120], [614384, 614400]],
        }])

    def test_inclusive_y_extent_adds_a_row(self):
        plan = observed(y=1, y_extent=1)
        self.assertEqual([row["logical_y"] for row in plan["rows"]], [1, 2])
        self.assertEqual(plan["source_pixels_read"], 8)

    def test_explicit_padding_counts_corner_pixels_once_and_preserves_guards(self):
        plan = observed(source_width=1, source_height=1, source_padding_pixels=3,
                        source_padding_rows=1, source_stride_bytes=24,
                        source_offset_bytes=8, source_buffer_bytes=56, y_extent=1)
        self.assertEqual(plan["padding_read_pixels"], 7)
        self.assertEqual(plan["source_pixels_read"], 8)
        self.assertEqual([(row["source_start"], row["source_end_exclusive"])
                          for row in plan["rows"]], [(8, 24), (32, 48)])
        source = bytearray([0xCC]) * 56
        # Red is the single active pixel; blue words are explicit pixel padding.
        for offset in (8, 12, 16, 20, 32, 36, 40, 44):
            word = 0xFFFF0000 if offset == 8 else 0xFF0000FF
            source[offset:offset + 4] = word.to_bytes(4, "big")
        unchanged_source = bytes(source)
        actual = materialize_synthetic_trace(plan, source)
        expected = bytearray([0xA5]) * 614400
        for offset in (0, 1280):
            expected[offset:offset + 16] = bytes.fromhex("f800f800" + "001f" * 6)
        for offset in (2560, 3840):
            expected[offset:offset + 16] = bytes.fromhex("001f" * 8)
        self.assertEqual(actual, expected)
        self.assertEqual(bytes(source), unchanged_source)

    def test_stride_slack_and_large_buffer_do_not_authorize_column_padding(self):
        with self.assertRaises(FramebufferModelError):
            observed(source_width=1, source_height=1,
                     source_stride_bytes=1280, source_buffer_bytes=307200)

    def test_buffer_capacity_alone_does_not_authorize_row_padding(self):
        with self.assertRaises(FramebufferModelError):
            observed(source_width=4, source_height=1, y_extent=1,
                     source_stride_bytes=16, source_buffer_bytes=32)

    def test_one_missing_declared_padding_pixel_is_rejected(self):
        with self.assertRaises(FramebufferModelError):
            observed(source_width=1, source_height=1, source_padding_pixels=2,
                     source_stride_bytes=16, source_buffer_bytes=16)

    def test_observed_rejects_invalid_origin_extent_and_unsigned_wrap(self):
        for changes in ({"x": 320}, {"y": 240}, {"x": -1}, {"y_extent": -1},
                        {"x_extent": 2 ** 32}, {"x": 1, "x_extent": 2 ** 32 - 1},
                        {"y": 1, "y_extent": 2 ** 32 - 1}):
            with self.subTest(changes=changes):
                with self.assertRaises(FramebufferModelError):
                    observed(**changes)

    def test_aligned_interior_hypothesis_matches_reference_accesses(self):
        safe = reference(x=4, y=3, width=8, height=2)
        hypothesis = observed(x=4, y=3, x_extent=7, y_extent=1)
        self.assertEqual(safe["rows"], hypothesis["rows"])
        self.assertEqual(safe["source_read_bytes"], hypothesis["source_read_bytes"])
        self.assertNotEqual(safe["mode"], hypothesis["mode"])


class LayoutAndScopeTests(unittest.TestCase):
    def test_both_models_reject_invalid_or_unbounded_layouts(self):
        changes_to_reject = (
            {"source_width": 0}, {"source_height": 0},
            {"source_width": 1025}, {"source_height": 1025},
            {"source_padding_pixels": -1}, {"source_padding_rows": -1},
            {"source_width": 1024, "source_padding_pixels": 1},
            {"source_height": 1024, "source_padding_rows": 1},
            {"source_stride_bytes": 1276}, {"source_stride_bytes": 1281},
            {"source_stride_bytes": 65540}, {"source_offset_bytes": 2},
            {"source_offset_bytes": -4}, {"source_buffer_bytes": 307199},
            {"source_buffer_bytes": 16 * 1024 * 1024 + 1},
            {"source_offset_bytes": 16 * 1024 * 1024 + 4},
        )
        for planner in (reference, observed):
            for changes in changes_to_reject:
                with self.subTest(planner=planner.__name__, changes=changes):
                    with self.assertRaises(FramebufferModelError):
                        planner(**changes)

    def test_full_declared_storage_must_fit_not_only_accessed_pixels(self):
        for planner in (reference, observed):
            with self.subTest(planner=planner.__name__):
                with self.assertRaises(FramebufferModelError):
                    planner(source_width=4, source_height=2,
                            source_stride_bytes=24, source_buffer_bytes=40)
                with self.assertRaises(FramebufferModelError):
                    planner(source_width=4, source_height=1, source_padding_rows=1,
                            source_stride_bytes=24, source_buffer_bytes=47)

    def test_non_integer_layouts_and_geometry_do_not_coerce(self):
        for planner in (reference, observed):
            for key in ("x", "y", "source_width", "source_height", "source_stride_bytes",
                        "source_buffer_bytes", "source_offset_bytes",
                        "source_padding_pixels", "source_padding_rows"):
                for value in (True, 1.0, "1", None):
                    with self.subTest(planner=planner.__name__, key=key, value=value):
                        with self.assertRaises(FramebufferModelError):
                            planner(**{key: value})
        for planner, key in ((reference, "width"), (reference, "height"),
                             (observed, "x_extent"), (observed, "y_extent")):
            with self.subTest(planner=planner.__name__, key=key):
                with self.assertRaises(FramebufferModelError):
                    planner(**{key: False})

    def test_metadata_is_json_safe_and_never_claims_abi_or_hardware_validation(self):
        arguments = dict(FULL_SOURCE)
        before = arguments.copy()
        plans = [plan_reference_blit(x=0, y=0, width=1, height=1, **arguments),
                 plan_observed_blit(x=0, y=0, x_extent=0, y_extent=0, **arguments)]
        self.assertEqual(arguments, before)
        for plan in plans:
            with self.subTest(mode=plan["mode"]):
                self.assertIs(plan["abi_verified"], False)
                self.assertIs(plan["hardware_tested"], False)
                self.assertIs(plan["caller_padding_contract_resolved"], False)
                self.assertEqual(plan["destination_offset_origin"],
                                 "pixel-base, excluding allocation prefix")
                self.assertEqual(json.loads(json.dumps(plan)), plan)


if __name__ == "__main__":
    unittest.main()
