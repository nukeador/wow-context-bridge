from __future__ import annotations

import unittest

from reader.protocol import (
    MAX_PAYLOAD,
    ProtocolError,
    decode_packet,
    encode_context,
    encode_packet,
    normalize_context,
    utf8_safe_truncate,
)


class ProtocolTests(unittest.TestCase):
    def test_ascii_context_round_trip(self) -> None:
        context = {"player": "SamplePlayer", "zone": "Elwynn Forest", "subzone": "Goldshire", "target": "Innkeeper Farley"}
        decoded = decode_packet(encode_context(context, 123))
        self.assertEqual(decoded.sequence, 123)
        self.assertEqual(decoded.context, context)

    def test_accented_and_non_latin_names_round_trip(self) -> None:
        context = {"player": "Álvaro", "zone": "Dun Morogh", "subzone": "Kharanos", "target": "Мария"}
        decoded = decode_packet(encode_context(context, 9))
        self.assertEqual(decoded.context, context)

    def test_empty_target_round_trip(self) -> None:
        context = {"player": "Aíra", "zone": "Elwynn Forest", "subzone": "", "target": ""}
        self.assertEqual(decode_packet(encode_context(context, 2)).context, context)

    def test_maximum_context_is_truncated_at_utf8_boundary(self) -> None:
        context = {"player": "界" * 500, "zone": "é" * 500, "subzone": "ß" * 500, "target": "NPC"}
        normalized = normalize_context(context)
        payload = encode_context(context, 1)[7:-2]
        self.assertLessEqual(len(payload), MAX_PAYLOAD)
        for value in normalized.values():
            value.encode("utf-8", errors="strict")
        self.assertTrue(normalized["player"].endswith("界") or normalized["player"] == "")

    def test_truncate_does_not_leave_partial_utf8_character(self) -> None:
        self.assertEqual(utf8_safe_truncate("ab界cd", 4), "ab")
        self.assertEqual(utf8_safe_truncate("ab界cd", 5), "ab界")

    def test_maximum_payload_size_and_overflow(self) -> None:
        packet = encode_packet(65535, b"x" * MAX_PAYLOAD)
        self.assertEqual(len(packet), 9 + MAX_PAYLOAD)
        with self.assertRaises(ProtocolError):
            encode_packet(0, b"x" * (MAX_PAYLOAD + 1))

    def test_version_validation(self) -> None:
        packet = encode_packet(4, b"{}", version=3)
        with self.assertRaisesRegex(ProtocolError, "version"):
            decode_packet(packet)

    def test_checksum_validation(self) -> None:
        packet = bytearray(encode_context({"player": "A", "zone": "B", "subzone": "", "target": ""}, 1))
        packet[-1] ^= 0x01
        with self.assertRaisesRegex(ProtocolError, "checksum"):
            decode_packet(bytes(packet))

    def test_truncated_and_malformed_packets(self) -> None:
        good = encode_context({"player": "A", "zone": "B", "subzone": "", "target": ""}, 1)
        with self.assertRaisesRegex(ProtocolError, "incomplete"):
            decode_packet(good[:8])
        with self.assertRaisesRegex(ProtocolError, "bad magic"):
            decode_packet(b"\0\0" + good[2:])
        with self.assertRaisesRegex(ProtocolError, "UTF-8"):
            decode_packet(encode_packet(1, b"\xff"))


if __name__ == "__main__":
    unittest.main()
