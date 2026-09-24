import ast
import json
from pathlib import Path
import sys
import unittest


REPOSITORY = Path(__file__).resolve().parents[1]
APPLICATION = REPOSITORY / "src" / "LunaTranslator"
sys.path.insert(0, str(APPLICATION))

from textio.regionoverlay import (  # noqa: E402
    DetectedTextRegion,
    RegionRect,
    RegionTracker,
    combine_detections,
    estimate_wrapped_layout,
    place_above_original,
    place_in_original,
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
        second = tracker.update([changed], "ja-en", "above", now=1.5)[0]
        self.assertEqual(first.region_id, second.region_id)
        self.assertGreater(second.generation, first.generation)
        tracker.update([], "ja-en", "above", now=2.0)
        self.assertEqual(1, len(tracker.snapshots()))
        tracker.touch("above", now=2.6)
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
    }

    def test_existing_profiles_receive_safe_defaults(self):
        config = json.loads(
            (APPLICATION / "defaultconfig" / "config.json").read_text(
                encoding="utf-8"
            )
        )
        expected = {
            "ocr_fullscreen_detection": False,
            "ocr_keep_regions_separate": True,
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
