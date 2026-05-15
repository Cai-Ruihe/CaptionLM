"""Session recorder for meeting transcripts (Phase 3).

Records all (timestamp, original, translated) entries and
supports export to SRT, CSV, and TXT formats.
"""

from __future__ import annotations

import csv
import io
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)


@dataclass
class SubtitleEntry:
    """A single subtitle entry."""
    timestamp: float        # Unix timestamp
    original: str
    translated: str
    source_lang: str
    target_lang: str
    provider: str
    cost_usd: float = 0.0   # cumulative for this entry (LLM providers only)


class SessionRecorder:
    """Records subtitle entries and exports to various formats."""

    def __init__(self):
        self._entries: list[SubtitleEntry] = []
        self._start_time: float | None = None

    def add_entry(self, entry: SubtitleEntry) -> None:
        if self._start_time is None:
            self._start_time = entry.timestamp
        self._entries.append(entry)

    def clear(self) -> None:
        self._entries.clear()
        self._start_time = None

    @property
    def entry_count(self) -> int:
        return len(self._entries)

    # Sentence-end punctuation that STT keeps revising at the trailing edge.
    # Includes both half-width and full-width forms used across CJK / Latin.
    _TRAILING_PUNCT = "。、？！?!.,…．・;；:： "

    # Time gap (seconds) below which two entries are considered the same
    # streaming burst. Empirical: STT partials arrive every ~50-300ms during
    # active speech, so 1.5s is a comfortable upper bound that won't merge
    # two distinct sentences spoken back-to-back with a natural pause.
    _BURST_GAP_S = 1.5

    @staticmethod
    def _collapse_partials(entries: list[SubtitleEntry]) -> list[SubtitleEntry]:
        """Collapse consecutive entries that belong to the same streaming
        burst (same utterance, multiple STT partials).

        Why this is not trivial: pipeline records HETEROGENEOUS text per
        partial. Sometimes it records just the new chunk after a sentence
        boundary ("今日？"), sometimes the full text after an utterance
        reset ("こんにちは。今日は？"). Naive prefix-extension matching
        breaks when pipeline switches between these modes inside one
        utterance, OR when STT revises trailing punctuation between
        partials ("？" → "とても。").

        Heuristic: collapse if BOTH conditions hold:
          1. Time gap (e.timestamp - prev.timestamp) < _BURST_GAP_S
             — same speech burst (rapid-fire partials).
          2. Content overlap exists: either e starts with prev's stripped
             original, OR prev's stripped original is contained in e.
             — guards against time-coincidence false positives.

        Edge cases:
        - Two distinct fast sentences ("Hi." pause "Bye.") — even if time
          gap is short, content won't overlap → both kept.
        - STT regression to shorter text — content overlap still works
          when prev's stripped prefix appears in e or when e's stripped
          prefix appears in prev. We swap-keep the longer one.
        - Exact duplicates dropped silently.
        """
        if not entries:
            return entries

        out: list[SubtitleEntry] = [entries[0]]
        for e in entries[1:]:
            prev = out[-1]

            # Drop exact duplicates outright.
            if e.original == prev.original and e.translated == prev.translated:
                continue

            time_gap = e.timestamp - prev.timestamp
            if time_gap >= SessionRecorder._BURST_GAP_S:
                out.append(e)
                continue

            prev_stripped = prev.original.rstrip(SessionRecorder._TRAILING_PUNCT)
            e_stripped = e.original.rstrip(SessionRecorder._TRAILING_PUNCT)

            # Empty prev/e after strip means it was all-punct — never collapse.
            if not prev_stripped or not e_stripped:
                out.append(e)
                continue

            overlap = (
                e.original.startswith(prev_stripped)
                or prev_stripped in e.original
                or e_stripped in prev.original
            )
            if not overlap:
                out.append(e)
                continue

            # Same burst, content overlaps → keep the LONGER of the two.
            # Empirically the later partial is usually longer (more text seen),
            # but STT regressions can produce a shorter revision; in that
            # case we prefer the longer earlier one.
            if len(e.original) >= len(prev.original):
                out[-1] = e
            # else keep prev unchanged

        # Pass 2: prefix-subsume cleanup.
        # The pairwise pass above only compares each new entry to the most
        # recently kept one. This leaves "orphan" early entries that are
        # legitimate prefixes of later entries but were separated by a
        # transient fragment in between. Example:
        #   1: こんにちは。
        #   2: 今日？           ← fragment, replaced by entry 3 in pass 1
        #   3: こんにちは。今日は？  ← but entry 1 was never re-checked
        # After pass 1: [entry_1, entry_3]. Entry 1's stripped form is a
        # prefix of entry 3 — so entry 1 is fully subsumed by entry 3 and
        # SRT review doesn't need it.
        #
        # Time window: 30s. Long enough to absorb a slow-paced utterance,
        # short enough to avoid false-merging across topic changes (e.g.
        # speaker says "Hello." then 5 minutes later "Hello. World!" —
        # we should NOT merge those).
        SUBSUME_WINDOW_S = 30.0
        cleaned: list[SubtitleEntry] = []
        for i, entry in enumerate(out):
            stripped = entry.original.rstrip(SessionRecorder._TRAILING_PUNCT)
            if not stripped:
                cleaned.append(entry)
                continue
            subsumed = False
            for later in out[i + 1:]:
                if later.timestamp - entry.timestamp >= SUBSUME_WINDOW_S:
                    break  # too far in time
                if (
                    len(later.original) > len(entry.original)
                    and later.original.startswith(stripped)
                ):
                    subsumed = True
                    break
            if not subsumed:
                cleaned.append(entry)
        return cleaned

    def export_srt(
        self,
        path: str,
        bilingual: bool = True,
        skip_errors: bool = True,
        collapse_partials: bool = True,
        default_duration_s: float = 3.0,
    ) -> int:
        """Export as SRT subtitle file.

        Returns: number of entries written.

        Args:
            path: output file path
            bilingual: True → original line + translated line per entry.
                       False → translated only.
            skip_errors: drop entries whose translation looks like an error
                placeholder (e.g. "[Gemini error] ...", "[Error] ...").
            collapse_partials: True → fold consecutive prefix-extension
                partials into a single entry per utterance (recommended).
                False → write every recorded partial verbatim (debug-grade).
            default_duration_s: end-time fallback for the LAST entry (no
                following entry to derive end from).

        End-time logic: each entry's end = next entry's start - 50ms (avoids
        exact-overlap rendering quirks). Last entry uses start + default_duration_s.
        """
        # Filter entries
        entries = [
            e for e in self._entries
            if not skip_errors
            or not (
                e.translated.startswith("[Gemini error]")
                or e.translated.startswith("[Error]")
                or e.provider == "error"
            )
        ]
        # Drop entries with empty translated text
        entries = [e for e in entries if e.translated.strip()]

        if collapse_partials:
            before = len(entries)
            entries = self._collapse_partials(entries)
            logger.debug(
                "SRT collapse: %d entries → %d after merging streaming partials",
                before, len(entries),
            )

        if not entries:
            logger.info("No entries to export to SRT (path=%s)", path)
            # Still create empty file so the user sees the artifact
            open(path, "w", encoding="utf-8").close()
            return 0

        start_t = self._start_time or entries[0].timestamp

        with open(path, "w", encoding="utf-8") as f:
            for i, entry in enumerate(entries, 1):
                rel_start = entry.timestamp - start_t
                if i < len(entries):
                    next_entry = entries[i]  # 1-indexed loop, so entries[i] is next
                    rel_end = max(
                        rel_start + 0.1,  # at least 100ms display
                        (next_entry.timestamp - start_t) - 0.05,
                    )
                else:
                    rel_end = rel_start + default_duration_s
                start = self._format_srt_time(rel_start)
                end = self._format_srt_time(rel_end)
                f.write(f"{i}\n")
                f.write(f"{start} --> {end}\n")
                if bilingual and entry.original.strip():
                    # Two-line subtitle: original on top (smaller, dim if
                    # the player supports styling), translated below.
                    f.write(f"{entry.original}\n{entry.translated}\n\n")
                else:
                    f.write(f"{entry.translated}\n\n")
        logger.info("Exported %d entries to SRT: %s", len(entries), path)
        return len(entries)

    def export_csv(self, path: str) -> None:
        """Export as CSV file."""
        with open(path, "w", encoding="utf-8", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["Timestamp", "Original", "Translated", "Provider"])
            for entry in self._entries:
                dt = datetime.fromtimestamp(entry.timestamp).strftime("%Y-%m-%d %H:%M:%S")
                writer.writerow([dt, entry.original, entry.translated, entry.provider])
        logger.info("Exported %d entries to CSV: %s", len(self._entries), path)

    def export_txt(self, path: str, mode: str = "bilingual") -> None:
        """Export as plain text.

        Args:
            mode: "bilingual", "original", or "translated"
        """
        with open(path, "w", encoding="utf-8") as f:
            for entry in self._entries:
                if mode == "original":
                    f.write(f"{entry.original}\n")
                elif mode == "translated":
                    f.write(f"{entry.translated}\n")
                else:  # bilingual
                    f.write(f"{entry.original}\n{entry.translated}\n\n")
        logger.info("Exported %d entries to TXT (%s): %s", len(self._entries), mode, path)

    @staticmethod
    def _format_srt_time(seconds: float) -> str:
        """Format seconds as SRT timestamp (HH:MM:SS,mmm)."""
        td = timedelta(seconds=max(0, seconds))
        hours = int(td.total_seconds() // 3600)
        minutes = int((td.total_seconds() % 3600) // 60)
        secs = int(td.total_seconds() % 60)
        millis = int((td.total_seconds() % 1) * 1000)
        return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"
