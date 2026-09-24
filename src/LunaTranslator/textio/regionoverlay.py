from __future__ import annotations

from dataclasses import dataclass
from collections import OrderedDict
from difflib import SequenceMatcher
from hashlib import sha256
import math
import threading
import time
import unicodedata


@dataclass(frozen=True)
class RegionRect:
    x: int
    y: int
    width: int
    height: int

    @property
    def right(self):
        return self.x + self.width

    @property
    def bottom(self):
        return self.y + self.height

    @property
    def area(self):
        return max(0, self.width) * max(0, self.height)

    @property
    def valid(self):
        return self.width > 0 and self.height > 0

    def intersection(self, other: "RegionRect"):
        left = max(self.x, other.x)
        top = max(self.y, other.y)
        right = min(self.right, other.right)
        bottom = min(self.bottom, other.bottom)
        return RegionRect(left, top, max(0, right - left), max(0, bottom - top))

    def iou(self, other: "RegionRect"):
        overlap = self.intersection(other).area
        union = self.area + other.area - overlap
        return overlap / union if union else 0.0

    def center_distance(self, other: "RegionRect"):
        dx = (self.x + self.width / 2) - (other.x + other.width / 2)
        dy = (self.y + self.height / 2) - (other.y + other.height / 2)
        return math.hypot(dx, dy)

    def translated(self, dx: int, dy: int):
        return RegionRect(self.x + dx, self.y + dy, self.width, self.height)


@dataclass(frozen=True)
class DetectedTextRegion:
    rect: RegionRect
    text: str
    confidence: float = 1.0


@dataclass(frozen=True)
class TranslationToken:
    region_id: str
    generation: int
    content_hash: str
    language_key: str


@dataclass(frozen=True)
class RegionSnapshot:
    region_id: str
    rect: RegionRect
    source_text: str
    translation: str
    confidence: float
    last_seen: float
    placement: str
    content_hash: str
    generation: int
    state: str = "active"
    first_seen: float = 0.0
    missing_count: int = 0
    detection_count: int = 1


@dataclass
class _TrackedRegion:
    region_id: str
    rect: RegionRect
    source_text: str
    translation: str
    confidence: float
    last_seen: float
    placement: str
    content_hash: str
    generation: int = 0
    translated_hash: str = ""
    pending_token: TranslationToken | None = None
    last_request: float = -math.inf
    seen_in_latest_frame: bool = True
    first_seen: float = 0.0
    detection_count: int = 1
    missing_count: int = 0
    missing_since: float | None = None
    state: str = "candidate"
    recent_rects: tuple[RegionRect, ...] = ()
    candidate_text: str = ""
    candidate_count: int = 0
    last_failure: str = ""
    frame_size: tuple[int, int] | None = None
    last_logged: str = ""
    last_logged_at: float = 0.0

    def snapshot(self):
        return RegionSnapshot(
            region_id=self.region_id,
            rect=self.rect,
            source_text=self.source_text,
            translation=self.translation,
            confidence=self.confidence,
            last_seen=self.last_seen,
            placement=self.placement,
            content_hash=self.content_hash,
            generation=self.generation,
            state=self.state,
            first_seen=self.first_seen,
            missing_count=self.missing_count,
            detection_count=self.detection_count,
        )


def content_hash(text: str, language_key: str):
    return sha256((language_key + "\0" + normalize_content(text)).encode("utf-8")).hexdigest()


def normalize_content(text: str):
    return " ".join(unicodedata.normalize("NFKC", text or "").split()).casefold()


def normalize_identity(text: str):
    normalized = normalize_content(text)
    return "".join(char for char in normalized
                   if not char.isspace() and not unicodedata.category(char).startswith("P"))


def similar_text(first: str, second: str, sensitivity="normal"):
    a, b = normalize_identity(first), normalize_identity(second)
    if a == b:
        return True
    if not a or not b:
        return False
    ratio = SequenceMatcher(None, a, b).ratio()
    threshold = {"strict": 0.92, "normal": 0.84, "relaxed": 0.75}.get(
        sensitivity, 0.84
    )
    return ratio >= threshold and abs(len(a) - len(b)) <= max(2, len(a) // 4)


def _match_score(old: _TrackedRegion, new: DetectedTextRegion):
    iou = old.rect.iou(new.rect)
    scale = max(1.0, math.hypot(old.rect.width, old.rect.height))
    proximity = max(0.0, 1.0 - old.rect.center_distance(new.rect) / (scale * 2.0))
    size_similarity = min(old.rect.width, new.rect.width) / max(1, old.rect.width, new.rect.width)
    size_similarity *= min(old.rect.height, new.rect.height) / max(
        1, old.rect.height, new.rect.height
    )
    text_similarity = SequenceMatcher(
        None, normalize_identity(old.source_text), normalize_identity(new.text)
    ).ratio()
    return iou * 0.43 + proximity * 0.27 + size_similarity * 0.16 + text_similarity * 0.14


def suppress_duplicate_detections(detections: list[DetectedTextRegion], translate_short_labels=False):
    """Discard overlapping duplicates and repeated compact status labels."""
    clean = []
    for item in detections:
        text = (item.text or "").strip()
        if not item.rect.valid or not text:
            continue
        if not translate_short_labels:
            meaningful = "".join(char for char in text if char.isalnum())
            if not meaningful or meaningful.isdigit():
                continue
            if len(meaningful) == 1 and meaningful.isascii():
                continue
        clean.append(item)
    ranked = sorted(
        clean,
        key=lambda item: (item.confidence, len(normalize_content(item.text)), item.rect.area),
        reverse=True,
    )
    retained = []
    for item in ranked:
        duplicate = False
        for other in retained:
            intersection = item.rect.intersection(other.rect).area
            containment = intersection / max(1, min(item.rect.area, other.rect.area))
            if containment < 0.75:
                continue
            a, b = normalize_identity(item.text), normalize_identity(other.text)
            if (item.rect.iou(other.rect) >= 0.85 or similar_text(a, b)
                or (min(len(a), len(b)) >= 3 and (a in b or b in a))):
                duplicate = True
                break
        if not duplicate:
            retained.append(item)
    if not translate_short_labels:
        counts = {}
        for item in retained:
            key = normalize_identity(item.text)
            counts[key] = counts.get(key, 0) + 1
        retained = [
            item for item in retained
            if not (normalize_identity(item.text).isascii()
                    and len(normalize_identity(item.text)) <= 4
                    and item.rect.width <= 80 and item.rect.height <= 28
                    and counts[normalize_identity(item.text)] > 1)
        ]
    return sorted(retained, key=lambda item: (item.rect.y, item.rect.x))


def group_logical_detections(detections: list[DetectedTextRegion]):
    """Join tightly stacked lines, without joining neighboring menu rows."""
    remaining = sorted(detections, key=lambda item: (item.rect.y, item.rect.x))
    grouped = []
    while remaining:
        first = remaining.pop(0)
        rect, text, confidence = first.rect, first.text, first.confidence
        while len(normalize_identity(text)) >= 3:
            next_index = None
            for index, candidate in enumerate(remaining):
                other = candidate.rect
                gap = other.y - rect.bottom
                overlap = max(0, min(rect.right, other.right) - max(rect.x, other.x))
                if (len(normalize_identity(candidate.text)) >= 3
                    and 0 <= gap <= max(3, min(rect.height, other.height) // 5)
                    and overlap >= min(rect.width, other.width) * 0.55
                    and abs(rect.x - other.x) <= max(12, min(rect.width, other.width) * 0.2)):
                    next_index = index
                    break
            if next_index is None:
                break
            next_item = remaining.pop(next_index)
            other = next_item.rect
            left, top = min(rect.x, other.x), min(rect.y, other.y)
            rect = RegionRect(left, top, max(rect.right, other.right) - left,
                              max(rect.bottom, other.bottom) - top)
            text += "\n" + next_item.text
            confidence = min(confidence, next_item.confidence)
        grouped.append(DetectedTextRegion(rect, text, confidence))
    return grouped


class RegionTracker:
    """Tracks independent OCR regions and owns translation cache/generation state."""

    def __init__(
        self, stale_timeout=1.25, retry_delay=2.0, stability="normal",
        cache_limit=512, debug=False, request_timeout=15.0,
    ):
        self.stale_timeout = max(0.1, float(stale_timeout))
        self.retry_delay = retry_delay
        self.stability = stability
        self.cache_limit = max(1, int(cache_limit))
        self.request_timeout = max(1.0, float(request_timeout))
        self.debug = debug
        self._regions: dict[str, _TrackedRegion] = {}
        self._cache: OrderedDict[tuple[str, str], str] = OrderedDict()
        self._inflight: dict[tuple[str, str], list[TranslationToken]] = {}
        self._next_id = 1
        self._lock = threading.RLock()

    def _log(self, message, region):
        if self.debug:
            now = time.monotonic()
            if region.last_logged == message and now - region.last_logged_at < 5.0:
                return
            region.last_logged = message
            region.last_logged_at = now
            print("Region {}: {}".format(region.region_id, message))

    def _cache_key(self, language_key, source_text):
        return language_key, normalize_content(source_text)

    def _cached(self, language_key, source_text):
        key = self._cache_key(language_key, source_text)
        value = self._cache.get(key, "")
        if value:
            self._cache.move_to_end(key)
        return value

    def _remember(self, key, translation):
        self._cache[key] = translation
        self._cache.move_to_end(key)
        while len(self._cache) > self.cache_limit:
            self._cache.popitem(last=False)

    def _new_id(self):
        value = "ocr-region-{}".format(self._next_id)
        self._next_id += 1
        return value

    def needs_confirmation(self):
        with self._lock:
            return any(region.state in ("candidate", "temporarily_missing") or
                       bool(region.candidate_count) for region in self._regions.values())

    def _purge_stale(self, now):
        for key, region in tuple(self._regions.items()):
            if region.state == "candidate" and region.missing_count:
                self._regions.pop(key, None)
            elif (region.state == "temporarily_missing" and region.missing_count >= 2
                  and region.missing_since is not None
                  and now - region.missing_since >= self.stale_timeout):
                region.state = "retired"
                self._log("retired after confirmed absence", region)
                self._regions.pop(key, None)
        for cache_key, tokens in tuple(self._inflight.items()):
            if not any(
                (region := self._regions.get(token.region_id))
                and region.pending_token == token
                for token in tokens
            ):
                self._inflight.pop(cache_key, None)

    def _match(self, detection, available, frame_size):
        candidates = []
        for key in available:
            region = self._regions[key]
            old_rect = region.rect
            if frame_size and region.frame_size and frame_size != region.frame_size:
                old_w, old_h = region.frame_size
                new_w, new_h = frame_size
                old_rect = RegionRect(
                    round(old_rect.x * new_w / max(1, old_w)),
                    round(old_rect.y * new_h / max(1, old_h)),
                    round(old_rect.width * new_w / max(1, old_w)),
                    round(old_rect.height * new_h / max(1, old_h)),
                )
            proxy = _TrackedRegion(region.region_id, old_rect, region.source_text,
                                   region.translation, region.confidence, region.last_seen,
                                   region.placement, region.content_hash)
            score = _match_score(proxy, detection)
            nearby = old_rect.center_distance(detection.rect) <= max(
                15, math.hypot(old_rect.width, old_rect.height) * 0.45
            )
            if score >= 0.38 and (old_rect.iou(detection.rect) >= 0.12 or nearby):
                candidates.append((score, key))
        return max(candidates)[1] if candidates else None

    def update(
        self,
        detections: list[DetectedTextRegion],
        language_key: str,
        placement: str,
        now: float | None = None,
        frame_size: tuple[int, int] | None = None,
    ):
        now = time.monotonic() if now is None else now
        clean = [d for d in detections if d.rect.valid and d.text and d.text.strip()]
        with self._lock:
            for region in self._regions.values():
                region.seen_in_latest_frame = False
            available = set(self._regions)
            matched: list[tuple[DetectedTextRegion, _TrackedRegion | None]] = []
            for detection in clean:
                key = self._match(detection, available, frame_size)
                if key:
                    available.remove(key)
                    matched.append((detection, self._regions[key]))
                else:
                    matched.append((detection, None))

            for detection, region in matched:
                digest = content_hash(detection.text, language_key)
                if region is None:
                    region = _TrackedRegion(
                        region_id=self._new_id(),
                        rect=detection.rect,
                        source_text=detection.text,
                        translation=self._cached(language_key, detection.text),
                        confidence=detection.confidence,
                        last_seen=now,
                        placement=placement,
                        content_hash=digest,
                        first_seen=now,
                        recent_rects=(detection.rect,),
                        frame_size=frame_size,
                    )
                    if detection.confidence >= 0.97 and len(normalize_identity(detection.text)) >= 5:
                        region.state = "active"
                    if region.translation:
                        region.translated_hash = digest
                    self._regions[region.region_id] = region
                    self._log("created as {}".format(region.state), region)
                    continue

                if region.state == "temporarily_missing":
                    self._log("restored during grace period", region)
                    region.state = "active"
                if region.state == "candidate":
                    region.detection_count += 1
                    if region.detection_count >= 2:
                        region.state = "active"
                        self._log("candidate promoted to active", region)
                old_rect = region.rect
                if (
                    old_rect.center_distance(detection.rect) > 2
                    or abs(old_rect.width - detection.rect.width) > 2
                    or abs(old_rect.height - detection.rect.height) > 2
                ):
                    region.rect = detection.rect
                region.recent_rects = (region.recent_rects + (detection.rect,))[-4:]
                region.frame_size = frame_size
                region.confidence = detection.confidence
                region.last_seen = now
                region.placement = placement
                region.seen_in_latest_frame = True
                region.missing_count = 0
                region.missing_since = None
                if similar_text(region.source_text, detection.text, self.stability):
                    if digest != region.content_hash:
                        self._log("OCR variant rejected as unstable", region)
                    region.candidate_text = ""
                    region.candidate_count = 0
                    continue
                if similar_text(region.candidate_text, detection.text, self.stability):
                    region.candidate_count += 1
                else:
                    region.candidate_text = detection.text
                    region.candidate_count = 1
                clearly_different = SequenceMatcher(
                    None, normalize_identity(region.source_text),
                    normalize_identity(detection.text),
                ).ratio() < 0.5
                if region.candidate_count < 2 and not (
                    detection.confidence >= 0.97 and clearly_different
                ):
                    continue
                region.source_text = region.candidate_text
                region.content_hash = content_hash(region.source_text, language_key)
                region.generation += 1
                region.pending_token = None
                region.last_request = -math.inf
                region.candidate_text = ""
                region.candidate_count = 0
                cached = self._cached(language_key, region.source_text)
                if cached:
                    region.translation = cached
                    region.translated_hash = region.content_hash
                    self._log("cached translation reused", region)

            for key in available:
                region = self._regions[key]
                region.missing_count += 1
                if region.missing_since is None:
                    region.missing_since = now
                    if region.state == "active":
                        region.state = "temporarily_missing"
                        self._log("entered temporarily missing state", region)

            self._purge_stale(now)
            return self._snapshots_unsafe()

    def touch(self, placement: str, now: float | None = None):
        """Marks a frame as unchanged without re-running OCR."""
        now = time.monotonic() if now is None else now
        with self._lock:
            for region in self._regions.values():
                region.placement = placement
                if region.seen_in_latest_frame:
                    region.last_seen = now
            return self._snapshots_unsafe()

    def expire(self, now: float | None = None):
        now = time.monotonic() if now is None else now
        with self._lock:
            return self._snapshots_unsafe()

    def _snapshots_unsafe(self):
        return tuple(
            region.snapshot()
            for region in sorted(
                self._regions.values(), key=lambda item: (item.rect.y, item.rect.x)
            )
            if region.state in ("active", "temporarily_missing")
        )

    def snapshots(self):
        with self._lock:
            return self._snapshots_unsafe()

    def pending_requests(
        self, language_key: str, now: float | None = None
    ) -> list[tuple[TranslationToken, str]]:
        now = time.monotonic() if now is None else now
        requests = []
        with self._lock:
            for region in self._regions.values():
                if region.state != "active":
                    continue
                if region.pending_token:
                    if now - region.last_request < self.request_timeout:
                        continue
                    old = region.pending_token
                    for key, tokens in tuple(self._inflight.items()):
                        if old in tokens:
                            self._inflight.pop(key, None)
                            for request in tokens:
                                waiting = self._regions.get(request.region_id)
                                if waiting and waiting.pending_token == request:
                                    waiting.pending_token = None
                                    waiting.generation += 1
                                    waiting.last_failure = "translation request timed out"
                            break
                    if region.pending_token == old:
                        region.pending_token = None
                        region.generation += 1
                        region.last_failure = "translation request timed out"
                desired_key = self._cache_key(language_key, region.source_text)
                if region.translation and region.translated_hash == region.content_hash:
                    continue
                cached = self._cached(language_key, region.source_text)
                if cached:
                    region.translation = cached
                    region.translated_hash = region.content_hash
                    continue
                if now - region.last_request < self.retry_delay:
                    continue
                token = TranslationToken(
                    region.region_id,
                    region.generation,
                    region.content_hash,
                    language_key,
                )
                region.pending_token = token
                region.last_request = now
                if desired_key in self._inflight:
                    self._inflight[desired_key].append(token)
                else:
                    self._inflight[desired_key] = [token]
                    requests.append((token, region.source_text))
        return requests

    def apply_translation(self, token: TranslationToken, translation: str):
        translation = (translation or "").strip()
        with self._lock:
            key = next((key for key, tokens in self._inflight.items() if token in tokens), None)
            tokens = self._inflight.pop(key, [token]) if key else [token]
            applied = False
            for request in tokens:
                region = self._regions.get(request.region_id)
                if (not region or region.state not in ("active", "temporarily_missing")
                    or region.pending_token != request or region.generation != request.generation
                    or region.content_hash != request.content_hash):
                    continue
                region.pending_token = None
                if translation:
                    region.translation = translation
                    region.translated_hash = region.content_hash
                    applied = True
            if applied and key:
                self._remember(key, translation)
            elif not applied:
                region = self._regions.get(token.region_id)
                if region:
                    self._log("stale asynchronous result discarded", region)
            return applied

    def translation_failed(self, token: TranslationToken):
        with self._lock:
            key = next((key for key, tokens in self._inflight.items() if token in tokens), None)
            tokens = self._inflight.pop(key, [token]) if key else [token]
            for request in tokens:
                region = self._regions.get(request.region_id)
                if region and region.pending_token == request:
                    region.pending_token = None
                    region.last_failure = "translation failed"

    def set_direct_translation(self, region_id: str, translation: str, language_key: str):
        """Stores text returned by OCR engines that perform translation themselves."""
        translation = (translation or "").strip()
        with self._lock:
            region = self._regions.get(region_id)
            if not region or not translation:
                return False
            region.translation = translation
            region.translated_hash = region.content_hash
            region.pending_token = None
            self._remember(self._cache_key(language_key, region.source_text), translation)
            return True

    def set_placement(self, placement: str):
        with self._lock:
            for region in self._regions.values():
                region.placement = placement
            return self._snapshots_unsafe()

    def rekey_language(self, language_key: str):
        with self._lock:
            for region in self._regions.values():
                digest = content_hash(region.source_text, language_key)
                if digest == region.content_hash:
                    continue
                region.content_hash = digest
                region.generation += 1
                region.pending_token = None
                region.last_request = -math.inf
                region.translation = self._cached(language_key, region.source_text)
                region.translated_hash = region.content_hash if region.translation else ""
            return self._snapshots_unsafe()

    def clear(self):
        with self._lock:
            self._regions.clear()
            self._inflight.clear()


def select_capture_area(mode: str, window: RegionRect | None, display: RegionRect,
                        manual: RegionRect | None):
    """Screen-pixel crop for the existing OCR capture pipeline."""
    if mode == "display":
        return display
    if mode == "window":
        return window if window and window.valid else None
    if mode == "content":
        if not manual or not manual.valid:
            return None
        selected = manual.intersection(
            window if window and window.valid else display
        )
        return selected if selected.valid else None
    return window if window and window.valid else display


def capture_to_logical(capture: RegionRect, display_pixels: RegionRect,
                       display_logical: RegionRect, pixel_ratio: float):
    ratio = max(0.1, pixel_ratio)
    return RegionRect(
        display_logical.x + round((capture.x - display_pixels.x) / ratio),
        display_logical.y + round((capture.y - display_pixels.y) / ratio),
        round(capture.width / ratio), round(capture.height / ratio),
    )


def advance_empty_scan(streak: int):
    """An isolated empty OCR result is inconclusive; consecutive ones count."""
    streak += 1
    return streak, streak >= 2


def union_rect(rects: list[RegionRect]):
    valid = [rect for rect in rects if rect.valid]
    if not valid:
        return RegionRect(0, 0, 0, 0)
    left = min(rect.x for rect in valid)
    top = min(rect.y for rect in valid)
    right = max(rect.right for rect in valid)
    bottom = max(rect.bottom for rect in valid)
    return RegionRect(left, top, right - left, bottom - top)


def combine_detections(detections: list[DetectedTextRegion]):
    clean = [
        detection
        for detection in detections
        if detection.rect.valid and bool(detection.text and detection.text.strip())
    ]
    if not clean:
        return []
    clean.sort(key=lambda item: (item.rect.y, item.rect.x))
    return [
        DetectedTextRegion(
            rect=union_rect([item.rect for item in clean]),
            text="\n".join(item.text for item in clean),
            confidence=min(item.confidence for item in clean),
        )
    ]


def clamp_rect(rect: RegionRect, bounds: RegionRect):
    width = min(rect.width, bounds.width)
    height = min(rect.height, bounds.height)
    x = min(max(rect.x, bounds.x), bounds.right - width)
    y = min(max(rect.y, bounds.y), bounds.bottom - height)
    return RegionRect(x, y, max(0, width), max(0, height))


def _overlap_area(rect: RegionRect, occupied: tuple[RegionRect, ...]):
    return sum(rect.intersection(item).area for item in occupied)


def place_above_original(
    source: RegionRect,
    bounds: RegionRect,
    overlay_width: int,
    overlay_height: int,
    gap: int,
    occupied: tuple[RegionRect, ...] = (),
):
    width = min(max(1, overlay_width), bounds.width)
    height = min(max(1, overlay_height), bounds.height)
    centered_x = source.x + (source.width - width) // 2
    candidates = [
        ("above", RegionRect(centered_x, source.y - gap - height, width, height)),
        ("below", RegionRect(centered_x, source.bottom + gap, width, height)),
        ("left", RegionRect(source.x - gap - width, source.y, width, height)),
        ("right", RegionRect(source.right + gap, source.y, width, height)),
    ]
    fitting = [
        item
        for item in candidates
        if item[1].x >= bounds.x and item[1].right <= bounds.right
        and item[1].y >= bounds.y and item[1].bottom <= bounds.bottom
    ]
    pool = fitting or candidates
    scored = []
    for index, (placement, candidate) in enumerate(pool):
        clamped = clamp_rect(candidate, bounds)
        scored.append((_overlap_area(clamped, occupied), index, placement, clamped))
    _, _, placement, result = min(scored)
    return placement, result


def place_in_original(source: RegionRect, bounds: RegionRect):
    return source.intersection(bounds)


def _wrap_line(text: str, chars_per_line: int):
    if chars_per_line <= 0:
        return []
    result = []
    for paragraph in text.splitlines() or [""]:
        words = paragraph.split(" ")
        line = ""
        for word in words:
            candidate = word if not line else line + " " + word
            if len(candidate) <= chars_per_line:
                line = candidate
                continue
            if line:
                result.append(line)
            while len(word) > chars_per_line:
                result.append(word[:chars_per_line])
                word = word[chars_per_line:]
            line = word
        result.append(line)
    return result


def estimate_wrapped_layout(
    text: str,
    width: int,
    height: int,
    padding: int,
    minimum_font_size: int,
    maximum_font_size: int = 24,
):
    """Pure layout estimate used by tests and as the Qt renderer's initial size."""
    usable_width = max(1, width - padding * 2)
    usable_height = max(1, height - padding * 2)
    for font_size in range(maximum_font_size, minimum_font_size - 1, -1):
        chars_per_line = max(1, int(usable_width / (font_size * 0.58)))
        lines = _wrap_line(text, chars_per_line)
        if len(lines) * font_size * 1.25 <= usable_height:
            return font_size, tuple(lines), True
    chars_per_line = max(1, int(usable_width / (minimum_font_size * 0.58)))
    return minimum_font_size, tuple(_wrap_line(text, chars_per_line)), False
