import pathlib
import sys
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import smoke_api  # noqa: E402


class SmokeProfileTests(unittest.TestCase):
    def test_smoke_script_expected_status_in_dev_profile(self):
        self.assertTrue(smoke_api.direct_tile_status_ok(200, "development", public_effective=True))
        self.assertTrue(smoke_api.direct_tile_status_ok(404, "development", public_effective=True))
        self.assertFalse(smoke_api.direct_tile_status_ok(403, "development", public_effective=True))

    def test_smoke_script_expected_status_in_production_profile(self):
        self.assertTrue(smoke_api.direct_tile_status_ok(403, "production", public_effective=False))
        self.assertTrue(smoke_api.direct_tile_status_ok(404, "production", public_effective=False))
        self.assertFalse(smoke_api.direct_tile_status_ok(200, "production", public_effective=False))

    def test_smoke_script_auto_uses_policy(self):
        self.assertTrue(smoke_api.direct_tile_status_ok(200, "auto", public_effective=True))
        self.assertFalse(smoke_api.direct_tile_status_ok(403, "auto", public_effective=True))
        self.assertTrue(smoke_api.direct_tile_status_ok(403, "auto", public_effective=False))


if __name__ == "__main__":
    unittest.main()
