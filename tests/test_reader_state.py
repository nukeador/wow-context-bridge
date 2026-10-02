from __future__ import annotations

import unittest

from reader.protocol import decode_packet, encode_context
from reader.state import ContextTracker


class ReaderStateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.context = {"player": "SamplePlayer", "zone": "Dun Morogh", "subzone": "Kharanos", "target": ""}

    def test_duplicate_sequence_is_ignored(self) -> None:
        tracker = ContextTracker(stale_after=8)
        frame = decode_packet(encode_context(self.context, 10))
        self.assertEqual(tracker.ingest(frame, 0.0), self.context)
        self.assertIsNone(tracker.ingest(frame, 1.0))
        self.assertEqual(tracker.last_advance, 0.0)

    def test_heartbeat_with_same_context_refreshes_without_duplicate_output(self) -> None:
        tracker = ContextTracker(stale_after=8)
        first = decode_packet(encode_context(self.context, 10))
        beat = decode_packet(encode_context(self.context, 11))
        self.assertEqual(tracker.ingest(first, 0.0), self.context)
        self.assertIsNone(tracker.ingest(beat, 3.0))
        self.assertEqual(tracker.last_advance, 3.0)
        self.assertIsNone(tracker.stale_event(10.9))

    def test_stale_context_detected_once_and_recovers(self) -> None:
        tracker = ContextTracker(stale_after=8)
        first = decode_packet(encode_context(self.context, 10))
        beat = decode_packet(encode_context(self.context, 11))
        tracker.ingest(first, 0.0)
        self.assertEqual(tracker.stale_event(8.1), 8.1)
        self.assertIsNone(tracker.stale_event(9.0))
        tracker.ingest(beat, 10.0)
        self.assertIsNone(tracker.stale_event(17.9))
        self.assertAlmostEqual(tracker.stale_event(18.1), 8.1)


if __name__ == "__main__":
    unittest.main()
