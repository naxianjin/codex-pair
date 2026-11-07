"""对准确读取器ETW配置复算并验证完整配对删除不能通过。"""
import copy
import importlib.util
import json
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1] / 'validation/bytecode-provenance-20261105'


def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class RestrictedEvidenceProfilesTests(unittest.TestCase):
    def test_historical_profile_retains_exact_pair_and_image_counts(self):
        combined = load('check-combined-evidence')
        raw = load('check-restricted-raw-modules')
        data = json.loads((ROOT / 'restricted-raw-module-evidence.json').read_text(encoding='utf-8-sig'))
        self.assertEqual(combined.check(data), (13, 46, 106))
        self.assertEqual(raw.check(data)['module_pairs'], 46)
        for checker in (combined, raw):
            with self.subTest(checker=checker.__name__), self.assertRaises(ValueError):
                checker.check(data, profile='loaded-reader-v1')
        result = raw.check(data)
        first = result['modules'][0]
        removed = copy.deepcopy(data)
        removed['Trace']['ModuleEvents'] = [x for x in removed['Trace']['ModuleEvents']
            if not (x['PID'] == first['pid'] and x['Properties'][0] == first['fields']['ModuleID'])]
        with self.assertRaises(ValueError):
            raw.check(removed)


if __name__ == '__main__':
    unittest.main()
