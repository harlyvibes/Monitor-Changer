import unittest

from monitor_changer import mccs

DELL = (
    "(prot(monitor)type(LCD)model(U2720Q)cmds(01 02 03 07 0C E3 F3)vcp(02 04 05 08 10 12 "
    "14(01 04 05 06 08 09 0B 0C) 16 18 1A 52 60(0F 11 1B) AA(01 02 04) AC AE B2 B6 C6 C8 C9 "
    "D6(01 04 05) DC(00 03 05) DF E0 E1 E2(00 1D 01 02 04 0E 12 14 23 24 27) F0(00 0C) F1 F2 FD)"
    "mswhql(1)asset_eep(40)mccs_ver(2.1))"
)

# Sloppy but real-world: spaces inside parens, no outer parens, trailing NUL.
LG = (
    "prot(monitor)type(lcd)model(LG ULTRAGEAR)cmds(01 02 03 0C E3 F3)"
    "vcp(02 04 05 08 10 12 14(05 08 0B ) 16 18 1A 52 60( 11 12 0F) AC AE B2 B6 C0 C6 C8 C9 "
    "D6(01 04) DF 62 8D F4 F5(00 01 02) F6(00 01 02) 4D 4E 4F 15(01 06 09 10 11 13 14 28 29 32 44 48) "
    "F7(00 01 02 03) F8(00 01) F9 E4 E5 E6 E7 E8 E9 EA EB EF FD(00 01) FE(00 01 02) FF)"
    "mccs_ver(2.1)mswhql(1)\x00"
)


class ParseCapabilitiesTest(unittest.TestCase):
    def test_dell(self):
        caps = mccs.parse_capabilities(DELL)
        self.assertEqual(caps["model"], "U2720Q")
        self.assertEqual(caps["mccs_ver"], "2.1")
        self.assertEqual(caps["vcp"][0x60], [0x0F, 0x11, 0x1B])
        self.assertEqual(caps["vcp"][0x10], [])
        self.assertEqual(caps["vcp"][0xE2][:3], [0x00, 0x1D, 0x01])
        self.assertIn(0xFD, caps["vcp"])

    def test_lg_without_outer_parens(self):
        caps = mccs.parse_capabilities(LG)
        self.assertEqual(caps["model"], "LG ULTRAGEAR")
        self.assertEqual(caps["vcp"][0x60], [0x11, 0x12, 0x0F])
        self.assertEqual(caps["vcp"][0x15][-1], 0x48)
        self.assertEqual(mccs.supported_inputs(LG), [0x11, 0x12, 0x0F])

    def test_missing_input_list(self):
        self.assertEqual(mccs.supported_inputs("(vcp(10 12 60 62))"), [])

    def test_empty_and_garbage(self):
        self.assertEqual(mccs.parse_capabilities(""), {})
        self.assertEqual(mccs.parse_capabilities(None), {})
        self.assertEqual(mccs.supported_inputs("not a caps string"), [])
        self.assertEqual(mccs.supported_inputs("(vcp(60(0F 11"), [0x0F, 0x11])

    def test_codes_without_spaces(self):
        self.assertEqual(mccs.parse_capabilities("(vcp(021060(0F11)))")["vcp"], {0x02: [], 0x10: [], 0x60: [0x0F, 0x11]})


class InputNamesTest(unittest.TestCase):
    def test_names(self):
        self.assertEqual(mccs.input_name(0x11), "HDMI 1")
        self.assertEqual(mccs.input_name(0x0F), "DisplayPort 1")
        self.assertEqual(mccs.input_name(0x90), "Input 0x90")

    def test_parse_input(self):
        self.assertEqual(mccs.parse_input("hdmi1"), 0x11)
        self.assertEqual(mccs.parse_input("HDMI 2"), 0x12)
        self.assertEqual(mccs.parse_input("dp1"), 0x0F)
        self.assertEqual(mccs.parse_input("DisplayPort-2"), 0x10)
        self.assertEqual(mccs.parse_input("usb-c"), 0x1B)
        self.assertEqual(mccs.parse_input("0x11"), 0x11)
        self.assertEqual(mccs.parse_input("17"), 0x11)
        for bad in ("hdmi9", "0x100", "-1", ""):
            with self.assertRaises(ValueError):
                mccs.parse_input(bad)


if __name__ == "__main__":
    unittest.main()
