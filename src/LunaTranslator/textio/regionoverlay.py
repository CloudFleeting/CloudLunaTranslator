from __future__ import annotations

from dataclasses import dataclass
from difflib import SequenceMatcher
from hashlib import sha256
import math
import threading
import time


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
    pending_token: TranslationToken | None = None
    last_request: float = -math.inf
    seen_in_latest_frame: bool = True

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
        )


def content_hash(text: str, language_key: str):
    return sha256((language_key + "\0" + text).encode("utf-8")).hexdigest()


def _match_score(old: _TrackedRegion, new: DetectedTextRegion):
    iou = old.rect.iou(new.rect)
    scale = max(1.0, math.hypot(old.rect.width, old.rect.height))
    proximity = max(0.0, 1.0 - old.rect.center_distance(new.rect) / (scale * 2.0))
    text_similarity = SequenceMatcher(None, old.source_text, new.text).ratio()
    return iou * 0.55 + proximity * 0.30 + text_similarity * 0.15


class RegionTracker:
    """Tracks independent OCR regions and owns translation cache/generation state."""

    def __init__(self, stale_timeout=1.75, retry_delay=2.0):
        self.stale_timeout = stale_timeout
        self.retry_delay = retry_delay
        self._regions: dict[str, _TrackedRegion] = {}
        self._cache: dict[tuple[str, str], str] = {}
        self._next_id = 1
        self._lock = threading.RLock()

    def _new_id(self):
        value = "ocr-region-{}".format(self._next_id)
        self._next_id += 1
        return value

    def _purge_stale(self, now):
        stale = [
            key
            for key, region in self._regions.items()
            if now - region.last_seen > self.stale_timeout
        ]
        for key in stale:
            self._regions.pop(key, None)

    def update(
        self,
        detections: list[DetectedTextRegion],
        language_key: str,
        placement: str,
        now: float | None = None,
    ):
        now = time.monotonic() if now is None else now
        clean = [
            detection
            for detection in detections
            if detection.rect.valid and bool(detection.text and detection.text.strip())
        ]
        with self._lock:
            for region in self._regions.values():
                region.seen_in_latest_frame = False
            available = set(self._regions)
            matched: list[tuple[DetectedTextRegion, _TrackedRegion | None]] = []
            for detection in clean:
                candidates = [
                    (key, _match_score(self._regions[key], detection))
                    for key in available
                ]
                candidates.sort(key=lambda item: item[1], reverse=True)
                if candidates and candidates[0][1] >= 0.38:
                    key = candidates[0][0]
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
                        translation=self._cache.get(
                            (language_key, detection.text), ""
                        ),
                        confidence=detection.confidence,
                        last_seen=now,
                        placement=placement,
                        content_hash=digest,
                    )
                    self._regions[region.region_id] = region
                    continue

                changed = region.content_hash != digest
                region.rect = detection.rect
                region.confidence = detection.confidence
                region.last_seen = now
                region.placement = placement
                region.seen_in_latest_frame = True
                if changed:
                    region.source_text = detection.text
                    region.content_hash = digest
                    region.generation += 1
                    region.pending_token = None
                    region.last_request = -math.inf
                    region.translation = self._cache.get(
                        (language_key, detection.text), ""
                    )

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
            self._purge_stale(now)
            return self._snapshots_unsafe()

    def expire(self, now: float | None = None):
        now = time.monotonic() if now is None else now
        with self._lock:
            self._purge_stale(now)
            return self._snapshots_unsafe()

    def _snapshots_unsafe(self):
        return tuple(
            region.snapshot()
            for region in sorted(
                self._regions.values(), key=lambda item: (item.rect.y, item.rect.x)
            )
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
                if region.translation or region.pending_token:
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
                requests.append((token, region.source_text))
        return requests

    def apply_translation(self, token: TranslationToken, translation: str):
        translation = (translation or "").strip()
        with self._lock:
            region = self._regions.get(token.region_id)
            if (
                not region
                or region.pending_token != token
                or region.generation != token.generation
                or region.content_hash != token.content_hash
            ):
                return False
            region.pending_token = None
            if not translation:
                return False
            region.translation = translation
            self._cache[(token.language_key, region.source_text)] = translation
            return True

    def translation_failed(self, token: TranslationToken):
        with self._lock:
            region = self._regions.get(token.region_id)
            if region and region.pending_token == token:
                region.pending_token = None

    def set_direct_translation(self, region_id: str, translation: str, language_key: str):
        """Stores text returned by OCR engines that perform translation themselves."""
        translation = (translation or "").strip()
        with self._lock:
            region = self._regions.get(region_id)
            if not region or not translation:
                return False
            region.translation = translation
            region.pending_token = None
            self._cache[(language_key, region.source_text)] = translation
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
                region.translation = self._cache.get(
                    (language_key, region.source_text), ""
                )
            return self._snapshots_unsafe()

    def clear(self):
        with self._lock:
            self._regions.clear()


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
    ]
    fitting = [
        item
        for item in candidates
        if item[1].y >= bounds.y and item[1].bottom <= bounds.bottom
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
