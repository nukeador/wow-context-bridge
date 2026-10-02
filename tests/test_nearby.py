import json
import unittest
from reader.protocol import encode_context, encode_packet, decode_packet, decode_image, ProtocolError, MAX_PAYLOAD
from reader.state import ContextTracker

class NearbyTests(unittest.TestCase):
    def test_unicode_dedup_and_v2(self):
        c = {"player": "A", "nearby": ["Мария", "Álvaro", "Мария", "A\"\\B"]}
        f = decode_packet(encode_context(c, 1))
        self.assertEqual(f.packet[2], 2)
        self.assertEqual(f.context["nearby"], sorted(set(c["nearby"])))
        self.assertEqual(f.context["nearby_total"], 3)

    def test_bounds_preserve_core_and_whole_names(self):
        names = ["NPC" + str(i) + "界" * 20 for i in range(40)]
        f = decode_packet(encode_context({"player": "Player", "target": "Target", "nearby": names}, 2))
        self.assertLessEqual(len(f.payload), MAX_PAYLOAD)
        self.assertLessEqual(len(f.context["nearby"]), 8)
        self.assertEqual(f.context["nearby_total"], 40)
        self.assertEqual(f.context["target"], "Target")
        self.assertTrue(all(n in names for n in f.context["nearby"]))

    def test_removed_names_and_empty_set(self):
        tracker = ContextTracker()
        tracker.ingest(decode_packet(encode_context({"nearby": ["NPC"]}, 1)), 0)
        result = tracker.ingest(decode_packet(encode_context({"nearby": []}, 2)), 1)
        self.assertEqual(result["nearby"], [])
        self.assertEqual(result["nearby_total"], 0)

    def test_schema_rejected(self):
        for names,total in [("NPC",1), ([123],1), (["NPC","NPC"],2), (["NPC"],0), ([], True), ([],41), (["X"]*9,9)]:
            payload=json.dumps({"nearby":names,"nearby_total":total}).encode()
            with self.assertRaises(ProtocolError):
                decode_packet(encode_packet(1,payload,version=2))

    def test_v2_synthetic_image(self):
        # Exercise the screenshot path, not only direct packet decoding.
        from reader.png import Image
        from reader.protocol import bytes_to_cells, COLUMNS
        packet=encode_context({"player":"A", "nearby":["Álvaro","Мария"]},7)
        cells=bytes_to_cells(packet);rows=(len(cells)+COLUMNS-1)//COLUMNS
        image=Image(COLUMNS*4,rows*4,bytearray(COLUMNS*4*rows*4*3))
        for i,v in enumerate(cells):
            for y in range((i//COLUMNS)*4,(i//COLUMNS+1)*4):
                for x in range((i%COLUMNS)*4,(i%COLUMNS+1)*4):
                    o=(y*image.width+x)*3
                    image.data[o:o+3]=bytes((255 if v&4 else 0,255 if v&2 else 0,255 if v&1 else 0))
        result=decode_image(image)
        self.assertEqual(result.status,"valid")
        self.assertEqual(result.frame.context["nearby"],["Álvaro","Мария"])
