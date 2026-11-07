"""受限进程身份、生命周期与映像观察关联的回归。"""
import copy
import importlib.util
from pathlib import Path
import unittest
import xml.etree.ElementTree as ET


SCRIPT = Path(__file__).resolve().parents[1] / 'validation/bytecode-provenance-20261105/check-system-image-run.py'
SPEC = importlib.util.spec_from_file_location('image_run', SCRIPT)
RUN = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(RUN)


class SystemImageRunTests(unittest.TestCase):
    def setUp(self):
        self.created = '2026-11-06T00:00:00.1234567Z'
        self.rows = []
        for pid, sequence, parent, name in ((1, 11, 99, 'python.exe'),
                                          (2, 12, 1, 'conhost.exe'),
                                          (3, 13, 1, 'powershell.exe')):
            fields = dict(ProcessID=str(pid), ProcessSequenceNumber=str(sequence),
                          ParentProcessID=str(parent), ParentProcessSequenceNumber='11' if parent == 1 else '99',
                          CreateTime=self.created, ImageName=name)
            self.rows.append((1, fields))
            self.rows.append((2, dict(fields, ExitCode='0')))
        self.rows.append((5, {'ProcessID': '3', 'ImageName': r'C:\Windows\System32\ntdll.dll',
                              'ImageBase': '0x10000', 'ImageSize': '0x2000'}))
        self.native = {'Formal': False, 'Native': {'ExitCode': 0, 'ActiveProcesses': 0,
            'TimedOut': False, 'TotalProcesses': 3, 'RootPid': 1,
            'RootCreationFileTime': RUN._filetime(self.created)},
            'Trace': {'Formal': False, 'Summary': 'Total Events Lost 0\n'}}
        self.capture = {'PID': 3, 'ProcessCreationUTC': self.created,
            'Images': [{'Name': 'ntdll.dll', 'BaseAddress': '65536', 'ImageSize': 8192}]}
        self.refresh()

    def refresh(self):
        namespace = 'http://schemas.microsoft.com/win/2004/08/events/event'
        root = ET.Element('Events')
        for event_id, fields in self.rows:
            event = ET.SubElement(root, '{' + namespace + '}Event')
            system = ET.SubElement(event, '{' + namespace + '}System')
            ET.SubElement(system, '{' + namespace + '}Provider',
                          Guid='{22fb2cd6-0e7b-422b-a0c7-2fad1fd0e716}')
            ET.SubElement(system, '{' + namespace + '}EventID').text = str(event_id)
            data = ET.SubElement(event, '{' + namespace + '}EventData')
            for name, value in fields.items():
                ET.SubElement(data, '{' + namespace + '}Data', Name=name).text = value
        self.native['Trace']['Xml'] = ET.tostring(root, encoding='unicode')

    def test_restricted_process_and_image_binding(self):
        checked = RUN.check_process_binding(self.native, self.capture)
        self.assertTrue(checked['process_binding_verified'])
        self.assertFalse(checked['formal'])

    def test_capture_native_loss_and_image_changes_rejected(self):
        for field, value in (('PID', 99), ('ProcessCreationUTC', '2026-11-06T00:00:00.1234568Z')):
            changed = copy.deepcopy(self.capture)
            changed[field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                RUN.check_process_binding(self.native, changed)
        for field, value in (('RootCreationFileTime', self.native['Native']['RootCreationFileTime'] + 1),
                             ('ExitCode', False), ('TotalProcesses', 4)):
            changed = copy.deepcopy(self.native)
            changed['Native'][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                RUN.check_process_binding(changed, self.capture)
        for summary in ('Total Events Lost 1\n', '', 'Total Events Lost 0\nTotal Events Lost 0\n'):
            changed = copy.deepcopy(self.native)
            changed['Trace']['Summary'] = summary
            with self.assertRaises(ValueError):
                RUN.check_process_binding(changed, self.capture)
        changed = copy.deepcopy(self.capture)
        changed['Images'][0]['BaseAddress'] = '65537'
        with self.assertRaises(ValueError):
            RUN.check_process_binding(self.native, changed)

    def test_missing_exit_duplicate_load_and_parent_changes_rejected(self):
        original = copy.deepcopy(self.rows)
        for kind in ('missing_exit', 'duplicate_load', 'parent'):
            self.rows = copy.deepcopy(original)
            if kind == 'missing_exit':
                self.rows.pop(5)
            elif kind == 'duplicate_load':
                self.rows.append(self.rows[-1])
            else:
                self.rows[4][1]['ParentProcessID'] = '99'
            self.refresh()
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                RUN.check_process_binding(self.native, self.capture)


if __name__ == '__main__':
    unittest.main()
