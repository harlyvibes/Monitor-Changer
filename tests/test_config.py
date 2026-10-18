import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from monitor_changer import Config, parse_code, write_template


def monitor(name="DELL U2720Q", inputs=(0x11, 0x0F, 0x1B), supported=True):
    return SimpleNamespace(name=name, inputs=list(inputs), supported=supported)


class ConfigTest(unittest.TestCase):
    def test_defaults(self):
        config = Config()
        self.assertEqual(config.poll_seconds, 30)
        self.assertFalse(config.hide_unsupported)
        self.assertEqual(config.label_for(monitor()), "DELL U2720Q")
        self.assertEqual(
            config.inputs_for(monitor()),
            [(0x0F, "DisplayPort 1"), (0x11, "HDMI 1"), (0x1B, "USB-C")],
        )

    def test_overrides(self):
        config = Config({"monitors": {"dell u2720q": {
            "label": "Left",
            "inputs": {"0x0F": "Desktop", "0x12": "Console", "17": "Laptop", "bogus": "x"},
            "hide": ["0x1B"],
        }}})
        m = monitor()
        self.assertEqual(config.label_for(m), "Left")
        self.assertEqual(
            config.inputs_for(m),
            [(0x0F, "Desktop"), (0x11, "Laptop"), (0x12, "Console")],
        )
        # Other monitors are unaffected.
        self.assertEqual(config.label_for(monitor("LG")), "LG")

    def test_power_off_override(self):
        config = Config({"monitors": {
            "DELL U2720Q": {"power_off": "0x05"},
            "LG": {"power_off": "nope"},
        }})
        self.assertEqual(config.power_off_value(monitor()), 0x05)
        self.assertIsNone(config.power_off_value(monitor("LG")))
        self.assertIsNone(config.power_off_value(monitor("Other")))

    def test_parse_code(self):
        self.assertEqual(parse_code("0x0F"), 15)
        self.assertEqual(parse_code("15"), 15)
        self.assertEqual(parse_code(17), 17)
        self.assertIsNone(parse_code("0F"))
        self.assertIsNone(parse_code("0x1FF"))

    def test_load_and_template(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sub" / "config.json"
            config, error = Config.load(path)
            self.assertIsNone(error)
            self.assertEqual(config.poll_seconds, 30)

            write_template([monitor(), monitor("Laptop", supported=False)], path)
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(list(data["monitors"]), ["DELL U2720Q"])
            self.assertEqual(data["monitors"]["DELL U2720Q"]["inputs"]["0x0F"], "DisplayPort 1")
            config, error = Config.load(path)
            self.assertIsNone(error)
            self.assertEqual(config.inputs_for(monitor())[0], (0x0F, "DisplayPort 1"))

            path.write_text("{not json", encoding="utf-8")
            config, error = Config.load(path)
            self.assertIn("invalid", error)
            self.assertEqual(config.poll_seconds, 30)


if __name__ == "__main__":
    unittest.main()
