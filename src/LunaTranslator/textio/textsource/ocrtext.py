import time, json
from myutils.config import globalconfig
from myutils.utils import checkmd5reloadmodule, parsekeystringtomodvkcode
import NativeUtils, windows
from gui.rangeselect import rangeadjust
from myutils.wrapper import threader
from myutils.ocrutil import imageCut, ocr_run, ocr_init
import time, gobject
from qtsymbols import *
from textio.textsource.textsourcebase import basetext
from ocrengines.baseocrclass import OCRResultParsed
from CVUtils import cvMat
from traceback import print_exc
from textio.regionoverlay import (
    DetectedTextRegion,
    RegionRect,
    RegionTracker,
    combine_detections,
)


def imageCutEx(hwnd, rectX: QRect):
    img = imageCut(hwnd, rectX)
    succ = True
    if hwnd:
        succ, img = img
    else:
        succ = False
    if img.isNull():
        return img
    if not succ:
        painter = QPainter(img)
        painter.setBrush(Qt.GlobalColor.white)
        painter.setPen(Qt.PenStyle.NoPen)
        rect2 = windows.GetWindowRect(gobject.base.translation_ui.winid)
        rect = QRect(rect2[0], rect2[1], rect2[2] - rect2[0], rect2[3] - rect2[1])
        if rectX.intersected(rect).isValid():
            rect.translate(-rectX.x(), -rectX.y())
            painter.drawRect(rect)
        try:
            rate = (
                NativeUtils.GetDevicePixelRatioF(hwnd)
                if hwnd
                else gobject.base.translation_ui.devicePixelRatioF()
            )
            for overlay in gobject.base.translation_ui.region_overlay_screen_rects():
                overlay_rect = QRect(
                    round(overlay.x * rate),
                    round(overlay.y * rate),
                    round(overlay.width * rate),
                    round(overlay.height * rate),
                )
                intersection = rectX.intersected(overlay_rect)
                if not intersection.isValid():
                    continue
                intersection.translate(-rectX.x(), -rectX.y())
                painter.setBrush(Qt.GlobalColor.white)
                painter.drawRect(intersection)
        except:
            pass
        painter.end()

    if globalconfig.get("use_ocr_preprocess", False):
        try:
            img = checkmd5reloadmodule(
                gobject.getconfig("ocr_preprocess.py"), "ocr_preprocess"
            ).Process(img)
        except:
            print_exc()
    return img


class rangemanger:
    def __init__(self, ref: "ocrtext", ranges: "list[rangemanger]"):
        self.ref = ref
        self.range_ui = rangeadjust(gobject.base.settin_ui, ranges)
        self.savelastimg: cvMat = None
        self.savelastrecimg: cvMat = None
        self.lastocrtime: float = 0
        self.savelasttext: str = None

    def __del__(self):
        self.range_ui.closesignal.emit()

    def getresmanual(self):
        rect = self.range_ui.getrect()
        if not rect.isValid():
            return
        imgr = imageCutEx(self.ref.hwnd, rect)
        if imgr.isNull():
            return
        result = ocr_run(imgr)
        self.savelastimg = cvMat(imgr)
        self.savelastrecimg = self.savelastimg
        self.lastocrtime = time.time()
        self.savelasttext = result.textonly
        return result

    def getresauto(self):
        rect = self.range_ui.getrect()
        if not rect.isValid():
            return
        imgr = imageCutEx(self.ref.hwnd, rect)
        ok = True
        if globalconfig.get("ocr_auto_method_v2", "period") == "analysis":
            imgr1 = cvMat(imgr)

            image_score = imgr1.MSSIM(self.savelastimg)

            gobject.base.thresholdsett1.emit(str(image_score))
            self.savelastimg = imgr1

            if image_score > globalconfig.get("ocr_stable_sim_v2", 0.5):

                image_score2 = imgr1.MSSIM(self.savelastrecimg)

                gobject.base.thresholdsett2.emit(str(image_score2))
                if image_score2 > globalconfig.get("ocr_diff_sim_v2", 0.95):
                    ok = False
                else:
                    self.savelastrecimg = imgr1
            else:
                ok = False
        elif globalconfig.get("ocr_auto_method_v2", "period") == "period":
            if time.time() - self.lastocrtime > globalconfig.get("ocr_interval", 1.5):
                ok = True
            else:
                ok = False
        if ok == False:
            return
        result = ocr_run(imgr)
        t = result.textonly
        self.lastocrtime = time.time()
        sim = NativeUtils.distance(self.savelasttext, t)
        self.savelasttext = t
        if sim < globalconfig.get("ocr_text_diff", 3):
            return
        self.savelasttext = t
        return result

    def waitforstable(self):
        rect = self.range_ui.getrect()
        if not rect.isValid():
            return False
        imgr = imageCutEx(self.ref.hwnd, rect)
        imgr1 = cvMat(imgr)
        image_score = imgr1.MSSIM(self.savelastimg)

        gobject.base.thresholdsett1.emit(str(float(image_score)))
        self.savelastimg = imgr1
        return image_score > globalconfig.get("ocr_stable_sim2_v2", 0.95)


class ocrtext(basetext):
    def hwndChanged(self, hwnd):
        changed = hwnd != self.hwnd
        self.hwnd = hwnd
        if changed and hasattr(self, "region_tracker"):
            self.region_tracker.clear()
            self._fullscreen_last_frame = None
            self._fullscreen_pending_change = True
            self._fullscreen_screen_name = None
            gobject.base.translation_ui.region_overlay_clear.emit()

    def init(self):
        self.hwnd = None
        self._pause_state = False
        threader(ocr_init)()
        self.ranges: "list[rangemanger]" = []
        self.region_tracker = RegionTracker(
            stale_timeout=globalconfig.get("ocr_region_stale_timeout", 1.75)
        )
        self._fullscreen_last_capture = 0.0
        self._fullscreen_last_ocr = 0.0
        self._fullscreen_last_frame: cvMat = None
        self._fullscreen_pending_change = False
        self._fullscreen_mode_active = False
        self._fullscreen_warned_no_boxes = False
        self._fullscreen_overlay_bounds = RegionRect(0, 0, 0, 0)
        self._region_last_language_key = None
        self._fullscreen_screen_name = None
        self.gettextthread()

    def _region_language_key(self):
        active = [
            key
            for key in globalconfig.get("fix_translate_rank_rank", [])
            if key in gobject.base.translators
        ]
        return json.dumps(
            (
                globalconfig.get("srclang4", "auto"),
                globalconfig.get("tgtlang4", "zh"),
                globalconfig.get("toppest_translator", ""),
                active,
            ),
            ensure_ascii=False,
        )

    def _fullscreen_context(self):
        if self.hwnd:
            if not NativeUtils.IsWindowViewable(self.hwnd):
                return None
            values = windows.GetClientRectScreen(self.hwnd) or windows.GetWindowRect(
                self.hwnd
            )
            if not values:
                return None
            left, top, right, bottom = values
            rate = max(0.1, float(NativeUtils.GetDevicePixelRatioF(self.hwnd)))
            capture = QRect(left, top, right - left, bottom - top)
            bounds = RegionRect(
                round(left / rate),
                round(top / rate),
                round((right - left) / rate),
                round((bottom - top) / rate),
            )
            return capture, bounds
        screen = None
        if self._fullscreen_screen_name:
            for candidate in QApplication.screens():
                if candidate.name() == self._fullscreen_screen_name:
                    screen = candidate
                    break
        if screen is None:
            screen = QApplication.screenAt(QCursor.pos()) or QApplication.primaryScreen()
        if screen is None:
            return None
        self._fullscreen_screen_name = screen.name()
        geometry = screen.geometry()
        rate = max(0.1, float(screen.devicePixelRatio()))
        capture = QRect(
            round(geometry.x() * rate),
            round(geometry.y() * rate),
            round(geometry.width() * rate),
            round(geometry.height() * rate),
        )
        bounds = RegionRect(
            geometry.x(), geometry.y(), geometry.width(), geometry.height()
        )
        return capture, bounds

    def _emit_region_overlays(self, snapshots, bounds):
        gobject.base.translation_ui.region_overlay_update.emit(snapshots, bounds)

    def _detections_from_result(
        self, result: OCRResultParsed, scale_x: float, scale_y: float
    ):
        detections = []
        if not result or result.error or not result.result.hasboxs:
            return detections
        for block in result.result.blocks:
            box = block.box4
            if not box:
                continue
            x1, y1, x2, y2 = box
            rect = RegionRect(
                round(x1 / scale_x),
                round(y1 / scale_y),
                round((x2 - x1) / scale_x),
                round((y2 - y1) / scale_y),
            )
            text = block.region_text
            if not result.result.isocrtranslate:
                text = result._100_f(text)
            detections.append(DetectedTextRegion(rect, text, 1.0))
        if not globalconfig.get("ocr_keep_regions_separate", True):
            detections = combine_detections(detections)
        return detections

    def _capture_fullscreen(self, capture_rect: QRect):
        if self.hwnd:
            captures = (
                lambda: NativeUtils.GdiGrabWindow(self.hwnd),
                lambda: NativeUtils.WinRT.capture_window(self.hwnd),
            )
            for capture in captures:
                try:
                    data = capture()
                except:
                    data = None
                if not data:
                    continue
                image = QImage.fromData(data)
                if not image.isNull():
                    return image
        return imageCutEx(self.hwnd, capture_rect)

    def _region_translation_callback(self, token, result):
        if result and self.region_tracker.apply_translation(token, result.result):
            self._emit_region_overlays(
                self.region_tracker.snapshots(), self._fullscreen_overlay_bounds
            )
        else:
            self.region_tracker.translation_failed(token)

    def _region_translation_error(self, token, error):
        provider = getattr(error, "id", None) or "unknown"
        print(
            "Region translation failed [{}] via provider {}".format(
                token.region_id, provider
            )
        )
        self.region_tracker.translation_failed(token)

    def _schedule_region_translations(self, language_key):
        preferred = globalconfig.get("toppest_translator") or None
        for token, text in self.region_tracker.pending_requests(language_key):
            try:
                gobject.base.textgetmethod(
                    text,
                    is_auto_run=False,
                    waitforresultcallback=lambda result, token=token: self._region_translation_callback(
                        token, result
                    ),
                    waitforresultcallbackengine=preferred,
                    erroroutput=lambda error, token=token: self._region_translation_error(
                        token, error
                    ),
                    statusok=False,
                    isolated=True,
                )
            except:
                self.region_tracker.translation_failed(token)
                print_exc()

    def _scan_fullscreen(self):
        context = self._fullscreen_context()
        if context is None:
            self._fullscreen_overlay_bounds = RegionRect(0, 0, 0, 0)
            self._emit_region_overlays((), self._fullscreen_overlay_bounds)
            return
        capture_rect, overlay_bounds = context
        self._fullscreen_overlay_bounds = overlay_bounds
        language_key = self._region_language_key()
        if language_key != self._region_last_language_key:
            self._region_last_language_key = language_key
            snapshots = self.region_tracker.rekey_language(language_key)
            self._emit_region_overlays(snapshots, overlay_bounds)
            self._schedule_region_translations(language_key)
        now = time.monotonic()
        if now - self._fullscreen_last_capture < globalconfig.get(
            "ocr_fullscreen_capture_interval", 0.35
        ):
            return
        self._fullscreen_last_capture = now
        image = self._capture_fullscreen(capture_rect)
        if image.isNull():
            snapshots = self.region_tracker.expire(now)
            self._emit_region_overlays(snapshots, overlay_bounds)
            return

        preview = image.scaledToWidth(
            min(360, image.width()), Qt.TransformationMode.SmoothTransformation
        )
        preview_mat = cvMat(preview)
        unchanged = False
        if self._fullscreen_last_frame is not None:
            similarity = preview_mat.MSSIM(self._fullscreen_last_frame)
            unchanged = similarity >= globalconfig.get(
                "ocr_fullscreen_change_threshold", 0.985
            )
        self._fullscreen_last_frame = preview_mat
        placement = globalconfig.get("ocr_translation_placement", "above")
        force_rescan = now - self._fullscreen_last_ocr >= globalconfig.get(
            "ocr_fullscreen_force_interval", 5.0
        )
        if not unchanged:
            self._fullscreen_pending_change = True
        if unchanged and not self._fullscreen_pending_change and not force_rescan:
            snapshots = self.region_tracker.touch(placement, now)
            self._emit_region_overlays(snapshots, overlay_bounds)
            return
        if now - self._fullscreen_last_ocr < max(
            0.1, globalconfig.get("ocr_interval", 1.5)
        ):
            self._emit_region_overlays(self.region_tracker.snapshots(), overlay_bounds)
            return

        self._fullscreen_last_ocr = now
        self._fullscreen_pending_change = False
        result = ocr_run(
            image,
            merge_lines=(
                False
                if globalconfig.get("ocr_keep_regions_separate", True)
                else None
            ),
        )
        if result.error:
            print("Full-screen OCR: " + result.errorstring())
            self._emit_region_overlays(self.region_tracker.expire(now), overlay_bounds)
            return
        if result and not result.result.hasboxs:
            if not self._fullscreen_warned_no_boxes:
                print("Full-screen OCR requires an OCR engine that returns text boxes.")
                self._fullscreen_warned_no_boxes = True
            self._emit_region_overlays(self.region_tracker.expire(now), overlay_bounds)
            return

        scale_x = max(0.1, image.width() / max(1, overlay_bounds.width))
        scale_y = max(0.1, image.height() / max(1, overlay_bounds.height))
        detections = self._detections_from_result(result, scale_x, scale_y)
        snapshots = self.region_tracker.update(
            detections, language_key, placement, now
        )
        if result.result.isocrtranslate:
            for snapshot in snapshots:
                self.region_tracker.set_direct_translation(
                    snapshot.region_id, snapshot.source_text, language_key
                )
            snapshots = self.region_tracker.snapshots()
        self._emit_region_overlays(snapshots, overlay_bounds)
        if not result.result.isocrtranslate:
            self._schedule_region_translations(language_key)

    def clearrange(self):
        self.ranges.clear()
        try:
            globalconfig.pop("ocrregions2")
        except:
            pass

    def leaveone(self):
        while len(self.ranges) > 1:
            self.ranges.pop(0)  # 直接[-1:]不知道为什么不work
        if self.ranges:
            self.ranges[0].range_ui.isfocus = False

    def newrangeadjustor(self):
        if len(self.ranges) == 0 or globalconfig.get("multiregion", False):
            self.ranges.append(rangemanger(self, self.ranges))

    def starttrace(self, pos):
        for _r in self.ranges:
            _r.range_ui.starttrace(pos)

    def traceoffset(self, curr):
        for _r in self.ranges:
            _r.range_ui.traceoffsetsignal.emit(curr)

    def setrect(self, rect: QRect):
        self.ranges[-1].range_ui.setrect(rect)

    def setstyle(self, *_):
        [_.range_ui.setstyle() for _ in self.ranges]

    def showhiderangeui(self, b):
        if b and len(self.ranges) == 0:
            for region in globalconfig.get("ocrregions2", []):
                if not region:
                    continue
                self.newrangeadjustor()
                self.setrect(QRect(*region))
            return
        for _ in self.ranges:
            _.range_ui.setmousetransp(False)

            if b:
                _r = _.range_ui.getrect()
                if _r:
                    _.range_ui.setrect(_r)
            else:
                _.range_ui.hide()

    @threader
    def gettextthread(self):
        laststate = tuple((0 for _ in range(len(globalconfig["ocr_trigger_events"]))))
        lastevents = json.dumps(globalconfig["ocr_trigger_events"])
        while not self.ending:
            if self._pause_state:
                time.sleep(0.1)
                continue
            if not self.isautorunning:
                time.sleep(0.1)
                continue
            fullscreen = globalconfig.get("ocr_fullscreen_detection", False)
            if fullscreen:
                self._fullscreen_mode_active = True
                try:
                    self._scan_fullscreen()
                except:
                    print_exc()
                time.sleep(0.05)
                continue
            if self._fullscreen_mode_active:
                self._fullscreen_mode_active = False
                self._fullscreen_last_frame = None
                self._fullscreen_pending_change = False
                self._region_last_language_key = None
                self._fullscreen_screen_name = None
                self.region_tracker.clear()
                gobject.base.translation_ui.region_overlay_clear.emit()
            rs = self.getuseranges()
            if not rs:
                time.sleep(0.1)
                continue
            if globalconfig.get("ocr_auto_method_v2", "period") == "trigger":
                triggered = False
                this = tuple(
                    (
                        windows.GetAsyncKeyState(
                            parsekeystringtomodvkcode(line["vkey"])[1]
                        )
                        for line in globalconfig["ocr_trigger_events"]
                    )
                )
                if lastevents != json.dumps(globalconfig["ocr_trigger_events"]):
                    laststate = this
                    lastevents = json.dumps(globalconfig["ocr_trigger_events"])
                    continue
                for _, line in enumerate(globalconfig["ocr_trigger_events"]):
                    event = line["event"]
                    press = this[_]
                    if ((event == 0) and (laststate[_] == 0) and press) or (
                        (event == 1) and laststate[_] and (press == 0)
                    ):
                        triggered = True
                        break
                laststate = this
                if triggered:
                    if self.hwnd:
                        for _ in range(2):
                            # 切换前台窗口
                            p1 = windows.GetWindowThreadProcessId(self.hwnd)
                            p2 = windows.GetWindowThreadProcessId(
                                windows.GetForegroundWindow()
                            )
                            triggered = p1 == p2
                            if triggered:
                                break
                            time.sleep(0.1)

                if triggered:

                    t1 = time.time()
                    while (not self.ending) and (
                        globalconfig.get("ocr_auto_method_v2", "period") == "trigger"
                    ):
                        time.sleep(0.1)
                        if time.time() - t1 >= globalconfig.get("ocr_trigger_delay", 0):
                            break
                    while (not self.ending) and (
                        globalconfig.get("ocr_auto_method_v2", "period") == "trigger"
                    ):
                        if self.waitforstablex():
                            break
                        time.sleep(0.1)
                    t = self.getallres(False)
                    if t:
                        self.dispatchtext(t)
                time.sleep(0.01)
            else:
                laststate = tuple(
                    (0 for _ in range(len(globalconfig["ocr_trigger_events"])))
                )
                t = self.getallres(True)
                if t:
                    self.dispatchtext(t)
                time.sleep(0.1)

    def waitforstablex(self):
        for range_ui in self.getuseranges():
            if not range_ui.waitforstable():
                return False
        return True

    def getuseranges(self):
        for r in self.ranges:
            if r.range_ui.isfocus:
                return [r]
        return self.ranges

    def getallres(self, auto):
        __text: "list[OCRResultParsed]" = []
        for r in self.getuseranges():

            if auto:
                _ = r.getresauto()
            else:
                _ = r.getresmanual()
            if _ is None:
                continue
            if _.error:
                _.displayerror()
                return
            __text.append(_)
        if not __text:
            return
        text = "\n".join(_.textonly for _ in __text)
        if __text[0].result.isocrtranslate:
            gobject.base.displayinfomessage(text, "<notrans>")
        else:
            return text

    def gettextonce(self):
        return self.getallres(False)

    def pause_recognition(self):
        self._pause_state = True

    def resume_recognition(self):
        self._pause_state = False

    def end(self):
        self.region_tracker.clear()
        gobject.base.translation_ui.region_overlay_clear.emit()
        globalconfig["ocrregions2"] = [
            _.range_ui.getrect().getRect() for _ in self.ranges
        ]
        self.ranges.clear()
