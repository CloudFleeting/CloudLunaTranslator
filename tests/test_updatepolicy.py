import json
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src" / "LunaTranslator"))

from myutils.updatepolicy import in_app_update_enabled  # noqa: E402


class ForkUpdatePolicyTests(unittest.TestCase):
    def test_existing_enabled_profile_cannot_install_official_binary(self):
        self.assertFalse(in_app_update_enabled(True))
        self.assertFalse(in_app_update_enabled(False))

    def test_default_profile_disables_binary_updates(self):
        config = json.loads(
            (ROOT / "src" / "LunaTranslator" / "defaultconfig" / "config.json")
            .read_text(encoding="utf-8")
        )
        self.assertFalse(config["autoupdate"])


if __name__ == "__main__":
    unittest.main()
