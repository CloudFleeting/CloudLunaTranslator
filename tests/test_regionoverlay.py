import ast
import importlib.util
import json
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import patch


REPOSITORY = Path(__file__).resolve().parents[1]
APPLICATION = REPOSITORY / "src" / "LunaTranslator"
sys.path.insert(0, str(APPLICATION))

from textio.regionoverlay import (  # noqa: E402
    DetectedTextRegion,
    RegionRect,
    RegionTracker,
    advance_empty_scan,
    capture_to_logical,
    crop_window_capture,
    combine_detections,
    estimate_wrapped_layout,
    group_logical_detections,
    place_above_original,
    place_in_original,
    select_capture_area,
    suppress_duplicate_detections,
)


class RegionTrackerTests(unittest.TestCase):
    def setUp(self):
        self.bounds = RegionRect(0, 0, 1920, 1080)
        self.detections = [
            DetectedTextRegion(RegionRect(50, 80, 220, 60), "Potion shop"),
            DetectedTextRegion(RegionRect(1450, 90, 340, 110), "Meet the guard"),
            DetectedTextRegion(RegionRect(470, 780, 800, 170), "Reach the bridge"),
        ]

    def test_unrelated_regions_are_independent_jobs(self):
        tracker = RegionTracker()
        snapshots = tracker.update(self.detections, "ja-en", "above", now=1.0)
        requests = tracker.pending_requests("ja-en", now=1.0)
        self.assertEqual(3, len(snapshots))
        self.assertEqual(
            {item.text for item in self.detections}, {text for _, text in requests}
        )
        self.assertEqual(3, len({item.region_id for item in snapshots}))

    def test_async_results_map_to_their_source_coordinates(self):
        tracker = RegionTracker()
        snapshots = tracker.update(self.detections, "ja-en", "above", now=1.0)
        expected = {item.source_text: item.rect for item in snapshots}
        requests = tracker.pending_requests("ja-en", now=1.0)
        for token, source in reversed(requests):
            self.assertTrue(tracker.apply_translation(token, "translated " + source))
        actual = {
            item.source_text: (item.rect, item.translation)
            for item in tracker.snapshots()
        }
        for source, rect in expected.items():
            self.assertEqual(rect, actual[source][0])
            self.assertEqual("translated " + source, actual[source][1])

    def test_region_ids_survive_reordered_detections(self):
        tracker = RegionTracker()
        first = tracker.update(self.detections, "ja-en", "above", now=1.0)
        ids = {item.source_text: item.region_id for item in first}
        moved = [
            DetectedTextRegion(
                RegionRect(
                    item.rect.x + 3,
                    item.rect.y + 2,
                    item.rect.width,
                    item.rect.height,
                ),
                item.text,
            )
            for item in reversed(self.detections)
        ]
        second = tracker.update(moved, "ja-en", "above", now=1.2)
        self.assertEqual(ids, {item.source_text: item.region_id for item in second})

    def test_stale_async_result_is_discarded(self):
        tracker = RegionTracker()
        tracker.update([self.detections[0]], "ja-en", "above", now=1.0)
        stale_token, _ = tracker.pending_requests("ja-en", now=1.0)[0]
        changed = DetectedTextRegion(self.detections[0].rect, "Weapon shop")
        tracker.update([changed], "ja-en", "above", now=2.0)
        tracker.update([changed], "ja-en", "above", now=2.2)
        self.assertFalse(tracker.apply_translation(stale_token, "Old translation"))
        self.assertEqual("", tracker.snapshots()[0].translation)

    def test_one_failed_region_does_not_block_another(self):
        tracker = RegionTracker()
        tracker.update(self.detections[:2], "ja-en", "above", now=1.0)
        requests = tracker.pending_requests("ja-en", now=1.0)
        tracker.translation_failed(requests[0][0])
        self.assertTrue(tracker.apply_translation(requests[1][0], "Guard quest"))
        translations = {
            item.source_text: item.translation for item in tracker.snapshots()
        }
        self.assertEqual("", translations[requests[0][1]])
        self.assertEqual("Guard quest", translations[requests[1][1]])

    def test_unchanged_and_reappearing_text_uses_cache(self):
        tracker = RegionTracker(stale_timeout=0.5)
        tracker.update([self.detections[0]], "ja-en", "above", now=1.0)
        token, _ = tracker.pending_requests("ja-en", now=1.0)[0]
        tracker.apply_translation(token, "Potion Shop")
        tracker.update([self.detections[0]], "ja-en", "above", now=1.2)
        self.assertEqual([], tracker.pending_requests("ja-en", now=1.2))
        tracker.update([], "ja-en", "above", now=2.0)
        tracker.update([], "ja-en", "above", now=2.6)
        self.assertEqual((), tracker.snapshots())
        tracker.update([self.detections[0]], "ja-en", "above", now=2.1)
        self.assertEqual("Potion Shop", tracker.snapshots()[0].translation)
        self.assertEqual([], tracker.pending_requests("ja-en", now=2.1))

    def test_placement_switch_updates_every_active_region(self):
        tracker = RegionTracker()
        tracker.update(self.detections, "ja-en", "above", now=1.0)
        snapshots = tracker.set_placement("inplace")
        self.assertEqual(3, len(snapshots))
        self.assertEqual({"inplace"}, {item.placement for item in snapshots})

    def test_language_change_invalidates_old_translation(self):
        tracker = RegionTracker()
        tracker.update([self.detections[0]], "ja-en", "above", now=1.0)
        old_token, _ = tracker.pending_requests("ja-en", now=1.0)[0]
        tracker.apply_translation(old_token, "Potion Shop")
        before = tracker.snapshots()[0]
        after = tracker.rekey_language("ja-fr")[0]
        self.assertEqual(before.region_id, after.region_id)
        self.assertEqual("", after.translation)
        self.assertGreater(after.generation, before.generation)
        self.assertEqual(1, len(tracker.pending_requests("ja-fr", now=1.1)))

    def test_changed_and_removed_regions_update(self):
        tracker = RegionTracker(stale_timeout=1.0)
        first = tracker.update([self.detections[0]], "ja-en", "above", now=1.0)[0]
        changed = DetectedTextRegion(self.detections[0].rect, "Weapon shop")
        tracker.update([changed], "ja-en", "above", now=1.5)
        second = tracker.update([changed], "ja-en", "above", now=1.7)[0]
        self.assertEqual(first.region_id, second.region_id)
        self.assertGreater(second.generation, first.generation)
        tracker.update([], "ja-en", "above", now=2.0)
        self.assertEqual(1, len(tracker.snapshots()))
        tracker.update([], "ja-en", "above", now=3.2)
        self.assertEqual((), tracker.snapshots())

    def test_empty_and_zero_sized_regions_are_ignored(self):
        tracker = RegionTracker()
        invalid = [
            DetectedTextRegion(RegionRect(0, 0, 0, 20), "text"),
            DetectedTextRegion(RegionRect(0, 0, 20, 0), "text"),
            DetectedTextRegion(RegionRect(0, 0, 20, 20), ""),
            DetectedTextRegion(RegionRect(0, 0, 20, 20), "   "),
        ]
        self.assertEqual((), tracker.update(invalid, "ja-en", "above", now=1.0))
        self.assertEqual([], tracker.pending_requests("ja-en", now=1.0))

    def test_separation_can_be_disabled_explicitly(self):
        combined = combine_detections(self.detections)
        self.assertEqual(1, len(combined))
        self.assertEqual(
            "Potion shop\nMeet the guard\nReach the bridge", combined[0].text
        )

    def test_multiline_logical_block_remains_one_region(self):
        tracker = RegionTracker()
        multiline = DetectedTextRegion(
            RegionRect(200, 400, 500, 120), "First line\nSecond line"
        )
        snapshots = tracker.update([multiline], "ja-en", "above", now=1.0)
        self.assertEqual(1, len(snapshots))
        self.assertEqual("First line\nSecond line", snapshots[0].source_text)
        requests = tracker.pending_requests("ja-en", now=1.0)
        self.assertEqual(1, len(requests))
        self.assertEqual(multiline.text, requests[0][1])


class StabilityTests(unittest.TestCase):
    def setUp(self):
        self.box = RegionRect(100, 100, 260, 48)
        self.first = DetectedTextRegion(self.box, "艦隊を編成してください。", 0.5)
        self.tracker = RegionTracker(stale_timeout=1.25)

    def activate(self):
        self.assertEqual((), self.tracker.update([self.first], "ja-en", "above", now=0.0))
        active = self.tracker.update([self.first], "ja-en", "above", now=0.2)[0]
        token, _ = self.tracker.pending_requests("ja-en", now=0.2)[0]
        self.assertTrue(self.tracker.apply_translation(token, "Organize your fleet."))
        return active.region_id

    def test_one_or_several_misses_keep_the_same_translation(self):
        region_id = self.activate()
        for now in (0.5, 0.9, 1.2):
            snapshot = self.tracker.update([], "ja-en", "above", now=now)[0]
            self.assertEqual(region_id, snapshot.region_id)
            self.assertEqual("temporarily_missing", snapshot.state)
            self.assertEqual("Organize your fleet.", snapshot.translation)
        restored = self.tracker.update([self.first], "ja-en", "above", now=1.3)[0]
        self.assertEqual(region_id, restored.region_id)
        self.assertEqual("active", restored.state)
        self.assertEqual(0, restored.missing_count)
        self.assertEqual([], self.tracker.pending_requests("ja-en", now=1.3))

    def test_confirmed_absence_retires_after_elapsed_grace(self):
        self.activate()
        self.tracker.update([], "ja-en", "above", now=0.5)
        self.assertEqual(1, len(self.tracker.update([], "ja-en", "above", now=1.5)))
        self.assertEqual((), self.tracker.update([], "ja-en", "above", now=1.76))

    def test_capture_failure_and_incomplete_cycle_do_not_count_as_misses(self):
        self.activate()
        for now in (0.5, 1.5, 3.0):
            self.assertEqual(1, len(self.tracker.expire(now)))
            self.assertEqual(1, len(self.tracker.touch("above", now)))
        self.assertEqual(0, self.tracker.snapshots()[0].missing_count)

    def test_one_empty_ocr_cycle_is_not_evidence_of_disappearance(self):
        self.activate()
        streak, confirmed = advance_empty_scan(0)
        self.assertFalse(confirmed)
        self.assertEqual("Organize your fleet.", self.tracker.snapshots()[0].translation)
        streak, confirmed = advance_empty_scan(streak)
        self.assertTrue(confirmed)
        self.tracker.update([], "ja-en", "above", now=1.0)
        self.assertEqual("Organize your fleet.", self.tracker.snapshots()[0].translation)

    def test_static_screen_stays_translated_for_thirty_seconds(self):
        region_id = self.activate()
        for tick in range(1, 121):
            snapshot = self.tracker.touch("above", now=tick / 4)[0]
            self.assertEqual(region_id, snapshot.region_id)
            self.assertEqual("Organize your fleet.", snapshot.translation)

    def test_jitter_and_small_ocr_variants_preserve_id_and_translation(self):
        region_id = self.activate()
        variants = ["艦隊を編成してください!", "艦隊を編成して ください。", "艦隊を編成してください。"]
        for index, text in enumerate(variants):
            jitter = RegionRect(101 + index % 2, 99 + index % 2, 260, 48)
            snapshot = self.tracker.update(
                [DetectedTextRegion(jitter, text, 0.5)], "ja-en", "above", now=0.5 + index * 0.2
            )[0]
            self.assertEqual(region_id, snapshot.region_id)
            self.assertEqual("Organize your fleet.", snapshot.translation)
            self.assertEqual(0, snapshot.generation)
            self.assertEqual([], self.tracker.pending_requests("ja-en", now=0.5 + index * 0.2))

    def test_real_change_waits_for_confirmation_and_keeps_old_translation(self):
        region_id = self.activate()
        changed = DetectedTextRegion(self.box, "次の海域へ進撃する。", 0.5)
        first = self.tracker.update([changed], "ja-en", "above", now=0.5)[0]
        self.assertEqual("Organize your fleet.", first.translation)
        self.assertEqual([], self.tracker.pending_requests("ja-en", now=0.5))
        second = self.tracker.update([changed], "ja-en", "above", now=0.8)[0]
        self.assertEqual(region_id, second.region_id)
        self.assertEqual("Organize your fleet.", second.translation)
        token, text = self.tracker.pending_requests("ja-en", now=0.8)[0]
        self.assertEqual(changed.text, text)
        self.assertTrue(self.tracker.apply_translation(token, "Advance to the next area."))
        self.assertEqual("Advance to the next area.", self.tracker.snapshots()[0].translation)

    def test_alternating_variants_do_not_launch_repeated_requests(self):
        self.activate()
        a = DetectedTextRegion(self.box, "次の海域へ進撃する。", 0.5)
        b = DetectedTextRegion(self.box, "前の海域へ撤退する。", 0.5)
        for i, detection in enumerate((a, b, a, b)):
            self.tracker.update([detection], "ja-en", "above", now=0.5 + i * 0.2)
            self.assertEqual([], self.tracker.pending_requests("ja-en", now=0.5 + i * 0.2))
        self.assertEqual("Organize your fleet.", self.tracker.snapshots()[0].translation)

    def test_failed_replacement_keeps_last_successful_translation(self):
        self.activate()
        changed = DetectedTextRegion(self.box, "次の海域へ進撃する。", 0.5)
        self.tracker.update([changed], "ja-en", "above", now=0.5)
        self.tracker.update([changed], "ja-en", "above", now=0.7)
        token, _ = self.tracker.pending_requests("ja-en", now=0.7)[0]
        self.tracker.translation_failed(token)
        self.assertEqual("Organize your fleet.", self.tracker.snapshots()[0].translation)
        self.assertEqual([], self.tracker.pending_requests("ja-en", now=1.0))
        self.assertEqual(1, len(self.tracker.pending_requests("ja-en", now=2.8)))

    def test_resize_uses_relative_coordinates_for_matching(self):
        first = self.tracker.update([self.first], "ja-en", "above", now=0.0,
                                    frame_size=(800, 600))
        self.assertEqual((), first)
        resized = DetectedTextRegion(RegionRect(150, 150, 390, 72), self.first.text, 0.5)
        snapshot = self.tracker.update([resized], "ja-en", "above", now=0.2,
                                       frame_size=(1200, 900))[0]
        self.assertEqual("ocr-region-1", snapshot.region_id)

    def test_duplicate_requests_are_coalesced_and_cache_is_bounded(self):
        tracker = RegionTracker(cache_limit=2)
        same = [DetectedTextRegion(RegionRect(x, 100, 180, 40), "同じ台詞", 0.5)
                for x in (10, 400)]
        tracker.update(same, "ja-en", "above", now=0.0)
        tracker.update(same, "ja-en", "above", now=0.2)
        requests = tracker.pending_requests("ja-en", now=0.2)
        self.assertEqual(1, len(requests))
        self.assertTrue(tracker.apply_translation(requests[0][0], "Same line"))
        self.assertEqual(["Same line", "Same line"],
                         [item.translation for item in tracker.snapshots()])
        tracker._remember(("ja-en", "second"), "Second")
        tracker._remember(("ja-en", "third"), "Third")
        self.assertLessEqual(len(tracker._cache), 2)
        self.assertEqual([], tracker.pending_requests("ja-en", now=0.5))

    def test_timed_out_request_cannot_replace_newer_result(self):
        tracker = RegionTracker(request_timeout=2.0)
        detection = DetectedTextRegion(self.box, self.first.text, 0.5)
        tracker.update([detection], "ja-en", "above", now=0.0)
        tracker.update([detection], "ja-en", "above", now=0.2)
        old_token, _ = tracker.pending_requests("ja-en", now=0.2)[0]
        new_token, _ = tracker.pending_requests("ja-en", now=2.3)[0]
        self.assertGreater(new_token.generation, old_token.generation)
        self.assertFalse(tracker.apply_translation(old_token, "Old late result"))
        self.assertTrue(tracker.apply_translation(new_token, "Current result"))
        self.assertEqual("Current result", tracker.snapshots()[0].translation)


class DetectionTests(unittest.TestCase):
    def test_duplicate_boxes_and_repeated_status_labels_are_filtered(self):
        blocks = [
            DetectedTextRegion(RegionRect(100, 100, 180, 30), "出撃せよ", 0.8),
            DetectedTextRegion(RegionRect(102, 101, 178, 29), "出撃せよ", 0.5),
            DetectedTextRegion(RegionRect(400, 100, 35, 18), "MIN", 0.5),
            DetectedTextRegion(RegionRect(500, 100, 35, 18), "MIN", 0.5),
            DetectedTextRegion(RegionRect(400, 160, 40, 18), "MAX", 0.5),
            DetectedTextRegion(RegionRect(500, 160, 40, 18), "MAX", 0.5),
            DetectedTextRegion(RegionRect(400, 220, 40, 18), "Full", 0.5),
            DetectedTextRegion(RegionRect(500, 220, 40, 18), "Full", 0.5),
        ]
        result = suppress_duplicate_detections(blocks)
        self.assertEqual(["出撃せよ"], [item.text for item in result])
        self.assertEqual(0.8, result[0].confidence)
        self.assertEqual(7, len(suppress_duplicate_detections(blocks, True)))

    def test_short_japanese_menu_items_remain_separate(self):
        blocks = [
            DetectedTextRegion(RegionRect(10, 20, 90, 25), "出撃"),
            DetectedTextRegion(RegionRect(300, 20, 90, 25), "出撃"),
        ]
        self.assertEqual(2, len(suppress_duplicate_detections(blocks)))

    def test_conflicting_ocr_readings_of_one_box_keep_the_better_one(self):
        blocks = [
            DetectedTextRegion(RegionRect(10, 20, 200, 30), "艦隊編成", 0.9),
            DetectedTextRegion(RegionRect(11, 20, 199, 30), "艦体偏成", 0.4),
        ]
        self.assertEqual([blocks[0]], suppress_duplicate_detections(blocks))

    def test_separate_labels_and_tight_multiline_blocks(self):
        blocks = [
            DetectedTextRegion(RegionRect(100, 100, 200, 25), "First dialogue line"),
            DetectedTextRegion(RegionRect(102, 127, 210, 25), "Second dialogue line"),
            DetectedTextRegion(RegionRect(500, 100, 200, 25), "Different menu item"),
        ]
        grouped = group_logical_detections(blocks)
        self.assertEqual(2, len(grouped))
        self.assertIn("First dialogue line\nSecond dialogue line", [item.text for item in grouped])

    def test_content_crop_excludes_browser_chrome(self):
        window = RegionRect(0, 0, 1920, 1080)
        viewport = RegionRect(350, 180, 1200, 720)
        display = RegionRect(0, 0, 1920, 1080)
        self.assertEqual(viewport, select_capture_area("content", window, display, viewport))
        self.assertEqual(window, select_capture_area("window", window, display, viewport))
        self.assertEqual(display, select_capture_area("display", window, display, viewport))
        self.assertIsNone(select_capture_area("content", window, display, None))

    def test_secondary_display_capture_maps_to_logical_coordinates(self):
        physical_display = RegionRect(1920, 0, 2560, 1440)
        logical_display = RegionRect(1920, 0, 1707, 960)
        crop = RegionRect(2070, 150, 1200, 600)
        self.assertEqual(
            RegionRect(2020, 100, 800, 400),
            capture_to_logical(crop, physical_display, logical_display, 1.5),
        )

    def test_browser_viewport_is_cropped_from_bound_window_image(self):
        client = RegionRect(0, 100, 1920, 980)
        viewport = RegionRect(350, 185, 1200, 720)
        self.assertEqual(
            RegionRect(350, 85, 1200, 720),
            crop_window_capture(
                client, viewport, RegionRect(0, 0, 1920, 980)
            ),
        )
        self.assertEqual(
            RegionRect(700, 170, 2400, 1440),
            crop_window_capture(
                client, viewport, RegionRect(0, 0, 3840, 1960)
            ),
        )
        self.assertFalse(
            crop_window_capture(
                client, RegionRect(0, 0, 120, 60), RegionRect(0, 0, 1920, 980)
            ).valid
        )


class OverlayLifecycleTests(unittest.TestCase):
    def test_existing_widget_survives_misses_and_in_place_updates(self):
        fake_qt = types.ModuleType("qtsymbols")
        fake_qt.QWidget = object
        fake_qt.QObject = object
        fake_config = types.ModuleType("myutils.config")
        fake_config.globalconfig = {
            "ocr_fullscreen_detection": True,
            "ocr_translation_placement": "above",
        }
        fake_windows = types.ModuleType("windows")
        module_path = APPLICATION / "gui" / "regionoverlay.py"
        spec = importlib.util.spec_from_file_location("regionoverlay_widget_test", module_path)
        module = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {
            "qtsymbols": fake_qt, "myutils.config": fake_config, "windows": fake_windows,
        }):
            spec.loader.exec_module(module)

        class FakeWidget:
            created = []

            def __init__(self):
                self.closed = 0
                self.hidden = 0
                self.content = None
                self.__class__.created.append(self)

            def measure_above(self, text, source_width, screen_width):
                return 160, 30

            def set_content(self, text, placement, geometry):
                self.content = text, placement, geometry

            def hide(self):
                self.hidden += 1

            def close(self):
                self.closed += 1

            def deleteLater(self):
                pass

        module.RegionOverlayWidget = FakeWidget
        tracker = RegionTracker(stale_timeout=1.25)
        detection = DetectedTextRegion(RegionRect(100, 100, 160, 30), "艦隊を編成してください", 0.5)
        tracker.update([detection], "ja-en", "above", now=0.0)
        tracker.update([detection], "ja-en", "above", now=0.2)
        token, _ = tracker.pending_requests("ja-en", now=0.2)[0]
        tracker.apply_translation(token, "Organize fleet")
        manager = module.RegionOverlayManager()
        bounds = RegionRect(0, 0, 800, 600)
        manager.update_regions(tracker.snapshots(), bounds)
        widget = FakeWidget.created[0]
        for now in (0.5, 0.9, 1.2):
            manager.update_regions(tracker.update([], "ja-en", "above", now=now), bounds)
            self.assertIs(widget, manager._widgets["ocr-region-1"])
            self.assertEqual(0, widget.hidden)
            self.assertEqual(0, widget.closed)
        manager.update_regions(tracker.update([detection], "ja-en", "above", now=1.3), bounds)
        self.assertIs(widget, manager._widgets["ocr-region-1"])
        manager.update_regions(tracker.update([], "ja-en", "above", now=2.0), bounds)
        manager.update_regions(tracker.update([], "ja-en", "above", now=3.3), bounds)
        self.assertEqual(1, widget.closed)


class PlacementTests(unittest.TestCase):
    def setUp(self):
        self.bounds = RegionRect(100, 50, 800, 600)

    def test_above_placement_near_center(self):
        source = RegionRect(350, 300, 220, 60)
        placement, result = place_above_original(
            source, self.bounds, 260, 80, 8
        )
        self.assertEqual("above", placement)
        self.assertEqual(source.y - 8, result.bottom)
        self.assertGreaterEqual(result.x, self.bounds.x)
        self.assertLessEqual(result.right, self.bounds.right)

    def test_above_placement_clamps_at_horizontal_edges(self):
        for source in (
            RegionRect(100, 250, 80, 40),
            RegionRect(820, 250, 80, 40),
        ):
            _, result = place_above_original(
                source, self.bounds, 280, 70, 6
            )
            self.assertGreaterEqual(result.x, self.bounds.x)
            self.assertLessEqual(result.right, self.bounds.right)

    def test_above_placement_falls_back_below(self):
        source = RegionRect(300, 55, 220, 50)
        placement, result = place_above_original(
            source, self.bounds, 240, 70, 10
        )
        self.assertEqual("below", placement)
        self.assertEqual(source.bottom + 10, result.y)

    def test_above_placement_avoids_an_existing_translation(self):
        source = RegionRect(300, 260, 220, 50)
        occupied = (RegionRect(290, 170, 240, 80),)
        placement, result = place_above_original(
            source, self.bounds, 240, 80, 10, occupied
        )
        self.assertEqual("below", placement)
        self.assertEqual(0, result.intersection(occupied[0]).area)

    def test_in_place_remains_inside_source_and_screen(self):
        source = RegionRect(120, 90, 260, 120)
        result = place_in_original(source, self.bounds)
        self.assertEqual(source, result)
        clipped_source = RegionRect(50, 20, 100, 100)
        clipped = place_in_original(clipped_source, self.bounds)
        self.assertGreaterEqual(clipped.x, self.bounds.x)
        self.assertGreaterEqual(clipped.y, self.bounds.y)
        self.assertLessEqual(clipped.right, clipped_source.right)
        self.assertLessEqual(clipped.bottom, clipped_source.bottom)

    def test_relative_region_tracks_a_moved_game_window(self):
        relative = RegionRect(200, 180, 240, 60)
        first_bounds = RegionRect(100, 50, 800, 600)
        moved_bounds = RegionRect(420, 240, 800, 600)
        first_source = relative.translated(first_bounds.x, first_bounds.y)
        moved_source = relative.translated(moved_bounds.x, moved_bounds.y)
        _, first = place_above_original(
            first_source, first_bounds, 260, 70, 8
        )
        _, moved = place_above_original(
            moved_source, moved_bounds, 260, 70, 8
        )
        self.assertEqual(moved_bounds.x - first_bounds.x, moved.x - first.x)
        self.assertEqual(moved_bounds.y - first_bounds.y, moved.y - first.y)

    def test_long_translation_wraps_and_scales_without_clipping(self):
        text = "A considerably longer translated sentence that must wrap cleanly."
        font_size, lines, fits = estimate_wrapped_layout(
            text, width=360, height=140, padding=8, minimum_font_size=10
        )
        self.assertTrue(fits)
        self.assertGreater(len(lines), 1)
        self.assertGreaterEqual(font_size, 10)
        self.assertEqual(text.replace(" ", ""), "".join(lines).replace(" ", ""))


class CompatibilityTests(unittest.TestCase):
    LOCALIZED_KEYS = {
        "全屏检测",
        "保持文本区域分离",
        "翻译位置",
        "原文上方",
        "原文位置",
        "背景不透明度",
        "文本内边距",
        "最小字体大小",
        "距原文距离",
        "全屏区域翻译",
        "捕获区域", "自动", "所选窗口", "游戏内容区域（所选OCR范围）",
        "整个显示器", "消失宽限时间（秒）", "文本稳定性",
        "严格", "普通", "宽松", "翻译短标签",
    }

    def test_existing_profiles_receive_safe_defaults(self):
        config = json.loads(
            (APPLICATION / "defaultconfig" / "config.json").read_text(
                encoding="utf-8"
            )
        )
        expected = {
            "ocr_fullscreen_detection": False,
            "ocr_region_capture_mode": "auto",
            "ocr_keep_regions_separate": True,
            "ocr_region_text_stability": "normal",
            "ocr_region_translate_short_labels": False,
            "ocr_region_stale_timeout": 1.25,
            "ocr_translation_placement": "above",
            "ocr_overlay_background_opacity": 70,
            "ocr_overlay_text_padding": 6,
            "ocr_overlay_min_font_size": 10,
            "ocr_overlay_gap": 6,
        }
        for key, value in expected.items():
            self.assertIn(key, config)
            self.assertEqual(value, config[key])

        config_source = APPLICATION / "myutils" / "config.py"
        tree = ast.parse(config_source.read_text(encoding="utf-8"))
        sync_node = next(
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "syncconfig"
        )
        namespace = {}
        exec(compile(ast.Module(body=[sync_node], type_ignores=[]), "config.py", "exec"), namespace)
        legacy_profile = {"minlength": 3, "unrelated_user_value": True}
        namespace["syncconfig"](legacy_profile, config)
        for key, value in expected.items():
            self.assertEqual(value, legacy_profile[key])
        self.assertTrue(legacy_profile["unrelated_user_value"])

    def test_manual_region_capture_path_remains_present(self):
        source_path = APPLICATION / "textio" / "textsource" / "ocrtext.py"
        tree = ast.parse(source_path.read_text(encoding="utf-8"))
        methods = {
            node.name: node
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        self.assertIn("getallres", methods)
        self.assertIn("getuseranges", methods)
        names = {
            node.func.attr
            for node in ast.walk(methods["gettextthread"])
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        }
        self.assertIn("getallres", names)

    def test_region_jobs_use_opt_in_isolated_translation(self):
        source_path = APPLICATION / "textio" / "textsource" / "ocrtext.py"
        tree = ast.parse(source_path.read_text(encoding="utf-8"))
        calls = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "textgetmethod"
        ]
        self.assertTrue(
            any(
                any(
                    keyword.arg == "isolated"
                    and isinstance(keyword.value, ast.Constant)
                    and keyword.value.value is True
                    for keyword in call.keywords
                )
                for call in calls
            )
        )

    def test_every_language_contains_overlay_labels(self):
        for path in (REPOSITORY / "src" / "files" / "lang").glob("*.json"):
            values = json.loads(path.read_text(encoding="utf-8"))
            self.assertTrue(
                self.LOCALIZED_KEYS.issubset(values),
                "missing overlay labels in {}".format(path.name),
            )


if __name__ == "__main__":
    unittest.main()
