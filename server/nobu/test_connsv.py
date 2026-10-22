"""connsv: per-client world address (NOBU_ADVERTISE_MAP). python -m unittest nobu.test_connsv -v"""
import unittest

from nobu import connsv

WORLDS = [dict(wid=1, name="Nobunaga", host="192.0.2.10", port=9110, status=1)]
AMAP = connsv.parse_advertise_map(
    "10.0.0.0/8=192.0.2.10, 100.64.0.0/10=198.51.100.60, 0.0.0.0/0=203.0.113.125")


class AdvertiseTest(unittest.TestCase):
    def host(self, peer, amap=AMAP):
        return connsv.worlds_for(peer, WORLDS, amap)[0]["host"]

    def test_lan_overlay_internet(self):
        self.assertEqual(self.host("10.1.2.50"), "192.0.2.10")
        self.assertEqual(self.host("100.64.3.4"), "198.51.100.60")
        self.assertEqual(self.host("203.0.113.7"), "203.0.113.125")

    def test_first_match_wins(self):
        amap = connsv.parse_advertise_map("0.0.0.0/0=1.2.3.4,10.0.0.0/8=192.0.2.10")
        self.assertEqual(self.host("10.1.2.50", amap), "1.2.3.4")

    def test_unset_or_unparseable_keeps_configured_host(self):
        self.assertEqual(self.host("203.0.113.7", []), "192.0.2.10")
        self.assertEqual(self.host("?"), "192.0.2.10")

    def test_v4_mapped_v6_peer(self):
        self.assertEqual(self.host("::ffff:203.0.113.7"), "203.0.113.125")

    def test_port_and_input_untouched(self):
        w = connsv.worlds_for("203.0.113.7", WORLDS, AMAP)[0]
        self.assertEqual((w["port"], w["name"]), (9110, "Nobunaga"))
        self.assertEqual(WORLDS[0]["host"], "192.0.2.10")


if __name__ == "__main__":
    unittest.main()
