"""对准确读取器ETW配置复算并验证完整配对删除不能通过。"""
import copy
import importlib.util
import json
from pathlib import Path
import unittest
import xml.etree.ElementTree as ET


ROOT = Path(__file__).resolve().parents[1] / 'validation/bytecode-provenance-20261105'


def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class RestrictedEvidenceProfilesTests(unittest.TestCase):
    def test_parser_profile_keeps_previous_image_count_and_rejects_deleted_image(self):
        from tests.test_validator_host import trace_fixture
        data = trace_fixture()
        combined = load('check-combined-evidence')
        raw = load('check-restricted-raw-modules')
        ns = {'e': 'http://schemas.microsoft.com/win/2004/08/events/event'}
        document = ET.fromstring(data['Trace']['Xml'])
        image = next(event for event in document
                     if event.findtext('e:System/e:EventID', namespaces=ns) == '5'
                     and any(item.get('Name') == 'ProcessID' and (item.text or '').strip() == '3936'
                             for item in event.findall('e:EventData/e:Data', ns)))
        event = copy.deepcopy(image)
        path = r'\Device\HarddiskVolume4\PairLab\Tools\python\DLLs\_uuid.pyd'
        next(item for item in event.findall('e:EventData/e:Data', ns)
             if item.get('Name') == 'ImageName').text = path
        document.append(event)
        data['Trace']['Xml'] = ET.tostring(document, encoding='unicode')
        data['Trace']['Images'].append({'Image': path, 'SHA256': 'a' * 64})
        self.assertEqual(combined.check(data, profile='loaded-reader-v2'), (13, 52, 111))
        self.assertEqual(raw.check(data, profile='loaded-reader-v2')['module_pairs'], 52)
        with self.assertRaises(ValueError):
            combined.check(data, profile='loaded-reader-v1')
        document.remove(event)
        data['Trace']['Xml'] = ET.tostring(document, encoding='unicode')
        with self.assertRaises(ValueError):
            combined.check(data, profile='loaded-reader-v2')

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
