"""固定宿主目录对账；全部证据与 Store 位于临时目录。"""
import base64
import copy
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

from scripts import pair_validator_host as host
from scripts.pair_validator_worker import validator_request, verify_validator_result
from tests.reader_fixture import reader_observation


def sha(data):
    return hashlib.sha256(data).hexdigest()


def trace_fixture():
    """在真实历史轨迹上增加新读取器的六组模块与四个映像观察。"""
    source = Path(__file__).resolve().parents[1] / 'validation/bytecode-provenance-20261105'
    data = json.loads((source / 'restricted-raw-module-evidence.json').read_bytes())
    ns = {'e': 'http://schemas.microsoft.com/win/2004/08/events/event'}
    document = ET.fromstring(data['Trace']['Xml'])
    events = list(document)
    roots = [event for event in events if event.findtext('e:System/e:EventID', namespaces=ns) == '154'
             and event.find('e:System/e:Execution', ns).get('ProcessID') == '3936']
    raw = next(item for item in data['Trace']['ModuleEvents'] if item['PID'] == 3936)
    for index in range(6):
        assembly = str(9000000 + index)
        for event_id, original in (('154', roots[0]), ('155', roots[0])):
            event = copy.deepcopy(original)
            event.find('e:System/e:EventID', ns).text = event_id
            for field in event.findall('e:UserData/*/*', ns):
                name = field.tag.split('}')[-1]
                if name == 'AssemblyID':
                    field.text = assembly
                elif name == 'FullyQualifiedAssemblyName':
                    field.text = 'ReaderFixture' + str(index)
            document.append(event)
        for event_id in (152, 153):
            item = copy.deepcopy(raw)
            item['Id'] = event_id
            item['Properties'][0] = str(8000000 + index)
            item['Properties'][1] = assembly
            item['Properties'][4] = 'reader-fixture-' + str(index) + '.dll'
            data['Trace']['ModuleEvents'].append(item)
    image = next(event for event in events if event.findtext('e:System/e:EventID', namespaces=ns) == '5'
                 and any(item.get('Name') == 'ProcessID' and (item.text or '').strip() == '3936'
                         for item in event.findall('e:EventData/e:Data', ns)))
    for index in range(4):
        event = copy.deepcopy(image)
        path = r'\Device\HarddiskVolume4\reader-fixture-' + str(index) + '.dll'
        next(item for item in event.findall('e:EventData/e:Data', ns) if item.get('Name') == 'ImageName').text = path
        document.append(event)
        data['Trace']['Images'].append({'Image': path, 'SHA256': 'a' * 64})
    data['Trace']['Xml'] = ET.tostring(document, encoding='unicode')
    return data


class MemoryStore:
    def __init__(self):
        self.calls = []
        self.saved = {}

    def record_validator_run(self, *args):
        self.calls.append(args)
        run, request, native, artifact, tools = args[5:]
        verify_validator_result(request, native, artifact)
        value = json.dumps([request, native, artifact, tools], sort_keys=True)
        duplicate = run in self.saved
        if duplicate and self.saved[run] != value:
            raise ValueError('Validator run already fixed')
        self.saved[run] = value
        return {'saved': True, 'duplicate': duplicate, 'run_id': run,
                'formal_checks_verified': False}


class ValidatorHostTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.package = self.root / 'package'
        self.source = self.package / 'validation/bytecode-provenance-20261105'
        self.source.mkdir(parents=True)
        (self.package / 'scripts').mkdir()
        (self.package / 'scripts/pair_validator_worker.py').write_bytes(b'fixed worker')
        (self.package / 'scripts/pair_reader_assembly.py').write_bytes(b'fixed parser')
        self.run_id = 'a' * 32
        self.runs = self.root / 'runs'
        self.run = self.runs / self.run_id
        (self.run / 'input').mkdir(parents=True)
        self.store = MemoryStore()
        assets = {name: ('fixed ' + name).encode() for name in (
            'validate-findings.cjs', 'validate-coverage-ledger.cjs', 'report-schema.json',
            'read-windows-input.cjs', 'read-windows-input.ps1', 'WindowsSafeInput.cs')}
        self.prepared = {'context': {'epoch': 1, 'execution': 'fixture'},
                         'original_report_digest': '1' * 64,
                         'stored_report_digest': '2' * 64,
                         'adapted_report_digest': '3' * 64, 'inputs': []}
        for name, field in (('validate-findings.cjs', 'findings'),
                            ('validate-coverage-ledger.cjs', 'coverage_ledger')):
            schema = 'assets/audit/report-schema.json' if field == 'findings' else None
            self.prepared['inputs'].append({'input_bytes': b'[]', 'contract': {
                'name': name, 'input_field': field, 'input_sha256': sha(b'[]'),
                'validator_path': 'assets/audit/' + name, 'validator_sha256': sha(assets[name]),
                'reader_assets': {'assets/audit/' + key: sha(assets[key]) for key in (
                    'read-windows-input.cjs', 'read-windows-input.ps1', 'WindowsSafeInput.cs')},
                'schema_path': schema, 'schema_sha256': sha(assets['report-schema.json']) if schema else None}})
        self.tools = {path: sha(path.encode()).upper() for path in host._TOOLS.values()}
        self.worker_request = validator_request(self.prepared, node_sha256=self.tools[host._TOOLS['node']].lower())
        snapshot = dict(assets, **{'findings.json': b'[]', 'coverage_ledger.json': b'[]',
                                  'request.json': self.encode(self.worker_request),
                                  'pair_validator_worker.py': b'fixed worker',
                                  'pair_reader_assembly.py': b'fixed parser'})
        snapshot.update({name: ('control ' + name).encode() for name in host._CONTROLS})
        for name, data in snapshot.items():
            (self.run / 'input' / name).write_bytes(data)
        self.baseline = {'vm_id': '3d337311-e8b0-4d7e-a746-f21f72e4cdb6',
            'tools': self.tools, 'controls': {name: sha(snapshot[name]) for name in host._CONTROLS},
            'limits': {'processes': 16, 'process_memory_mib': 384, 'job_memory_mib': 512,
                       'cpu_seconds': 120, 'wall_seconds': 180, 'disk_quota_mib': 64}}
        for name in host._SOURCES:
            if name == 'root-identity-baseline.json':
                data = self.encode(self.baseline)
            elif name in ('check-combined-evidence.py', 'check-restricted-raw-modules.py'):
                data = (Path(__file__).resolve().parents[1] / 'validation/bytecode-provenance-20261105' / name).read_bytes()
            else:
                data = ('source ' + name).encode()
            (self.source / name).write_bytes(data)
        self.inventory = {'formal': False, 'profile': 'loaded-reader-v1',
                          'files': {name: sha((self.source / name).read_bytes()) for name in host._SOURCES}}
        self.write(self.source / 'raw-module-source-inventory-loaded-reader.json', self.inventory)
        self.outer = {'Run': self.run_id, 'VM': self.baseline['vm_id'], 'Formal': False,
            'EvidenceProfile': 'loaded-reader-v1', 'Limits': dict(host._LIMITS),
            'SourceInventorySHA256': sha(self.encode(self.inventory)).upper(),
            'ControllerSHA256': self.inventory['files']['Invoke-ValidatorRawModuleDiagnostic.ps1'].upper(),
            'Files': {name: sha(data).upper() for name, data in snapshot.items()}}
        self.write(self.run / 'request.json', self.outer)
        observation = reader_observation(sha(assets['WindowsSafeInput.cs']), pid=3948,
                                         process_creation_utc='2026-11-06T03:39:19.4908710Z')
        observed = {}
        results = []
        trace = trace_fixture()
        ns = {'e': 'http://schemas.microsoft.com/win/2004/08/events/event'}
        starts = [{item.get('Name'): (item.text or '').strip() for item in event.findall('e:EventData/e:Data', ns)}
                  for event in ET.fromstring(trace['Trace']['Xml'])
                  if event.findtext('e:System/e:EventID', namespaces=ns) == '1']
        for name, pid in (('validate-findings.cjs', 3948), ('validate-coverage-ledger.cjs', 3936)):
            value = copy.deepcopy(observation)
            start = next(item for item in starts if item['ProcessID'] == str(pid))
            value.update(pid=pid, process_creation_utc=start['CreateTime'][:-3] + 'Z')
            stderr = 'PAIR_READER_OBSERVATION:' + json.dumps(value) + '\n'
            observed[name] = [value]
            results.append({'validator': name, 'exit_code': 0, 'stdout_sha256': sha(b''),
                            'stderr': stderr, 'stderr_sha256': sha(stderr.encode())})
        self.artifact = {'binding': self.worker_request['binding'], 'files': self.worker_request['files'],
            'formal_checks_verified': False, 'validators_succeeded': True,
            'reader_observations': observed, 'results': results}
        self.native = {'Run': self.run_id, 'Files': copy.deepcopy(self.outer['Files']),
            'Tools': copy.deepcopy(self.tools), 'Trace': trace['Trace'],
            'Native': {'ExitCode': 0, 'ActiveProcesses': 0, 'TotalProcesses': 13,
                       'RootPid': trace['Native']['RootPid'], 'RootCreationFileTime': trace['Native']['RootCreationFileTime'], 'TimedOut': False,
                       'Seconds': 1.0, 'CPUSeconds': 0.5}}
        self.receipt = {'Run': self.run_id, 'State': 'terminal', 'Formal': False,
            'ControllerSHA256': self.outer['ControllerSHA256'],
            'SourceInventorySHA256': self.outer['SourceInventorySHA256']}
        self.refresh()
        for target, value in (('_PACKAGE_ROOT', self.package), ('_RUN_ROOT', self.runs)):
            patcher = patch.object(host, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        prepared = patch.object(host, 'saved_validator_inputs', return_value=self.prepared)
        self.prepare = prepared.start()
        self.addCleanup(prepared.stop)

    @staticmethod
    def encode(value):
        return json.dumps(value, sort_keys=True).encode()

    def write(self, path, value):
        path.write_bytes(self.encode(value))

    def refresh(self):
        data = self.encode(self.artifact)
        (self.run / 'artifact.json').write_bytes(data)
        self.native['Native']['ArtifactBase64'] = base64.b64encode(data).decode()
        self.write(self.run / 'native.json', self.native)
        self.receipt['NativeSHA256'] = sha(self.encode(self.native)).upper()
        self.write(self.run / 'receipt.json', self.receipt)

    def reconcile(self, **kwargs):
        return host.reconcile_restricted_validator_run(self.store, 'A', 'session', 'cwd',
                                                       'execution', 'lease', **dict(run_id=self.run_id, **kwargs))

    def test_reads_bound_run_and_preserves_diagnostic_status(self):
        result = self.reconcile()
        self.assertTrue(result['saved'])
        self.assertTrue(result['validators_succeeded'])
        self.assertFalse(result['formal_checks_verified'])
        self.assertEqual(result['status'], 'needs_validation')
        self.assertIn('host_identity_unverified', result['remaining_checks'])
        native = self.store.calls[0][7]
        self.assertNotIn('ArtifactBase64', native)
        self.assertEqual(native['HostObservation']['native_sha256'], sha(self.encode(self.native)))
        self.assertEqual(self.store.calls[0][-1]['node'], self.worker_request['node_sha256'])
        self.prepare.assert_called_with(self.store, 'A', 'session', 'cwd', 'execution', 'lease')
        self.assertTrue(self.reconcile()['duplicate'])
        self.native['Native']['CPUSeconds'] = 0.75
        self.refresh()
        with self.assertRaisesRegex(ValueError, 'already fixed'):
            self.reconcile()

    def test_run_reference_rejects_paths_and_extra_arguments(self):
        for value in ('../' + self.run_id, self.run_id.upper(), '', 'a' * 33, 123, None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                host.reconcile_restricted_validator_run(self.store, 'A', 's', 'c', 'e', 'l', run_id=value)
        with self.assertRaises(TypeError):
            self.reconcile(native={})
        self.assertEqual(self.store.calls, [])

    def test_installed_package_without_development_evidence_is_unavailable(self):
        self.source.rename(self.source.with_name('saved-validation'))
        with self.assertRaisesRegex(ValueError, 'installed host capability unavailable'):
            self.reconcile()
        self.assertEqual(self.store.calls, [])

    def test_validator_rejection_is_saved_as_failure_observation(self):
        result = self.artifact['results'][1]
        result['exit_code'] = 1
        result['stderr'] = 'Invalid coverage ledger'
        result['stderr_sha256'] = sha(result['stderr'].encode())
        self.artifact['reader_observations'][result['validator']] = []
        self.artifact['validators_succeeded'] = False
        self.refresh()
        saved = self.reconcile()
        self.assertTrue(saved['saved'])
        self.assertFalse(saved['validators_succeeded'])
        self.assertFalse(saved['formal_checks_verified'])
        self.assertEqual(saved['status'], 'needs_validation')

    def test_receipt_and_artifact_tampering_never_saved(self):
        files = ('request.json', 'receipt.json', 'native.json', 'artifact.json')
        original = {name: (self.run / name).read_bytes() for name in files}
        cases = ('receipt_run', 'receipt_digest', 'controller', 'inventory', 'formal', 'formal_int',
                 'native_run', 'native_files', 'native_tools', 'artifact_bytes', 'base64')
        for case in cases:
            for name, data in original.items():
                (self.run / name).write_bytes(data)
            if case in ('receipt_run', 'receipt_digest', 'controller', 'inventory', 'formal', 'formal_int'):
                value = json.loads(original['receipt.json'])
                key, changed = {'receipt_run': ('Run', 'b' * 32), 'receipt_digest': ('NativeSHA256', '0' * 64),
                    'controller': ('ControllerSHA256', '0' * 64), 'inventory': ('SourceInventorySHA256', '0' * 64),
                    'formal': ('Formal', True), 'formal_int': ('Formal', 0)}[case]
                value[key] = changed
                self.write(self.run / 'receipt.json', value)
            elif case == 'artifact_bytes':
                (self.run / 'artifact.json').write_bytes(original['artifact.json'] + b' ')
            else:
                value = json.loads(original['native.json'])
                if case == 'native_run':
                    value['Run'] = 'b' * 32
                elif case == 'native_files':
                    value['Files']['reader.cs'] = '0' * 64
                elif case == 'native_tools':
                    value['Tools'][host._TOOLS['node']] = '0' * 64
                else:
                    value['Native']['ArtifactBase64'] = 'invalid@'
                self.write(self.run / 'native.json', value)
                receipt = json.loads(original['receipt.json'])
                receipt['NativeSHA256'] = sha(self.encode(value))
                self.write(self.run / 'receipt.json', receipt)
            with self.subTest(case=case), self.assertRaises(ValueError):
                self.reconcile()
        self.assertEqual(self.store.calls, [])

    def test_current_inputs_sources_controls_and_report_are_required(self):
        for path in (self.run / 'input/findings.json', self.run / 'input/runner.cs',
                     self.package / 'scripts/pair_validator_worker.py',
                     self.package / 'scripts/pair_reader_assembly.py', self.source / 'Test-RootTrace.ps1'):
            original = path.read_bytes()
            path.write_bytes(original + b' changed')
            with self.subTest(path=path.name), self.assertRaises(ValueError):
                self.reconcile()
            path.write_bytes(original)
        self.prepared['context']['epoch'] = 2
        with self.assertRaises(ValueError):
            self.reconcile()
        self.prepared['context']['epoch'] = True
        with self.assertRaises(ValueError):
            self.reconcile()
        self.assertEqual(self.store.calls, [])

    def test_host_scope_profile_limits_and_trace_are_diagnostic_only(self):
        original = copy.deepcopy(self.outer)
        for field, value in (('Run', 'b' * 32), ('VM', 'other'), ('Formal', 0), ('Formal', True),
                             ('EvidenceProfile', 'historical'), ('Limits', dict(host._LIMITS, Processes=True))):
            changed = dict(original, **{field: value})
            self.write(self.run / 'request.json', changed)
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                self.reconcile()
        self.write(self.run / 'request.json', original)
        for value in (True, 0, None):
            self.native['Trace']['Formal'] = value
            self.refresh()
            with self.subTest(trace_formal=value), self.assertRaises(ValueError):
                self.reconcile()
        self.assertEqual(self.store.calls, [])

    def test_source_inventory_cannot_add_paths_or_claim_formal_authentication(self):
        path = self.source / 'raw-module-source-inventory-loaded-reader.json'
        for case in ('formal', 'profile', 'traversal', 'digest'):
            changed = copy.deepcopy(self.inventory)
            if case == 'formal':
                changed['formal'] = 0
            elif case == 'profile':
                changed['profile'] = 'historical'
            elif case == 'traversal':
                changed['files']['../escape'] = '0' * 64
            else:
                changed['files']['Test-RootTrace.ps1'] = '0' * 64
            self.write(path, changed)
            with self.subTest(case=case), self.assertRaises(ValueError):
                self.reconcile()
        self.assertEqual(self.store.calls, [])

    def test_rehashed_incomplete_etw_and_wrong_reader_binding_are_rejected(self):
        original = copy.deepcopy(self.native)
        artifact = copy.deepcopy(self.artifact)
        ns = {'e': 'http://schemas.microsoft.com/win/2004/08/events/event'}
        for case in ('empty_trace', 'lost', 'ambiguous_loss', 'root_creation', 'missing_exit',
                     'exit_failure', 'duplicate_start', 'wrong_reader_pid', 'wrong_reader_creation',
                     'duplicate_reader', 'missing_module_pair', 'missing_image_hash', 'invalid_image_hash'):
            self.native = copy.deepcopy(original)
            self.artifact = copy.deepcopy(artifact)
            if case == 'empty_trace':
                self.native['Trace'] = {'Formal': False}
            elif case in ('lost', 'ambiguous_loss'):
                self.native['Trace']['Summary'] = ('Total Events Lost 1\n' if case == 'lost'
                                                   else 'Total Events Lost 0\nTotal Events Lost 0\n')
            elif case == 'root_creation':
                self.native['Native']['RootCreationFileTime'] += 1
            elif case in ('missing_exit', 'exit_failure', 'duplicate_start'):
                document = ET.fromstring(self.native['Trace']['Xml'])
                event_id = '1' if case == 'duplicate_start' else '2'
                target_pid = str(self.native['Native']['RootPid']) if case == 'exit_failure' else '3936'
                target = next(event for event in document
                              if event.findtext('e:System/e:EventID', namespaces=ns) == event_id
                              and any(item.get('Name') == 'ProcessID' and (item.text or '').strip() == target_pid
                                      for item in event.findall('e:EventData/e:Data', ns)))
                if case == 'missing_exit':
                    document.remove(target)
                elif case == 'duplicate_start':
                    document.append(copy.deepcopy(target))
                else:
                    next(item for item in target.findall('e:EventData/e:Data', ns)
                         if item.get('Name') == 'ExitCode').text = '1'
                self.native['Trace']['Xml'] = ET.tostring(document, encoding='unicode')
            elif case.startswith('wrong_reader') or case == 'duplicate_reader':
                names = list(self.artifact['reader_observations'])
                observation = self.artifact['reader_observations'][names[0]][0]
                if case == 'wrong_reader_pid':
                    observation['pid'] = 999999
                elif case == 'wrong_reader_creation':
                    observation['process_creation_utc'] = '2026-11-06T00:00:00.0000000Z'
                else:
                    other = self.artifact['reader_observations'][names[1]][0]
                    observation.update(pid=other['pid'], process_creation_utc=other['process_creation_utc'])
                result = self.artifact['results'][0]
                result['stderr'] = 'PAIR_READER_OBSERVATION:' + json.dumps(observation) + '\n'
                result['stderr_sha256'] = sha(result['stderr'].encode())
            elif case == 'missing_module_pair':
                self.native['Trace']['ModuleEvents'] = [item for item in self.native['Trace']['ModuleEvents']
                                                       if item['Properties'][0] != '8000000']
            elif case == 'invalid_image_hash':
                self.native['Trace']['Images'][0]['SHA256'] = 'g' * 64
            else:
                self.native['Trace']['Images'] = []
            self.refresh()
            with self.subTest(case=case), self.assertRaises(ValueError):
                self.reconcile()
        self.assertEqual(self.store.calls, [])

    def test_native_terminal_types_resources_and_missing_observations_rejected(self):
        original = copy.deepcopy(self.native)
        cases = (('ExitCode', False), ('ActiveProcesses', 1), ('TimedOut', True),
                 ('TotalProcesses', True), ('TotalProcesses', 17), ('RootPid', 0),
                 ('RootPid', 2**32), ('RootCreationFileTime', 2**64),
                 ('Seconds', True), ('CPUSeconds', 121), ('Seconds', float('nan')))
        for field, value in cases:
            self.native = copy.deepcopy(original)
            self.native['Native'][field] = value
            self.refresh()
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                self.reconcile()
        self.native = original
        for item in self.artifact['results']:
            item['stderr'] = ''
            item['stderr_sha256'] = sha(b'')
        self.artifact.pop('reader_observations')
        self.refresh()
        with self.assertRaisesRegex(ValueError, 'reader observation missing'):
            self.reconcile()
        self.assertEqual(self.store.calls, [])

    def test_rehashed_changed_controls_inputs_and_false_formal_are_rejected(self):
        originals = {name: (self.run / 'input' / name).read_bytes() for name in
                     ('findings.json', 'runner.cs', 'pair_validator_worker.py', 'pair_reader_assembly.py', 'request.json')}
        for name, data in originals.items():
            (self.run / 'input' / name).write_bytes(data + b' ')
            self.outer['Files'][name] = sha(data + b' ')
            self.native['Files'] = copy.deepcopy(self.outer['Files'])
            self.write(self.run / 'request.json', self.outer)
            self.refresh()
            if name == 'request.json':
                # 仅序列化空白变化不改变请求；随后修改请求中的准确报告绑定。
                request = copy.deepcopy(self.worker_request)
                request['binding']['context']['epoch'] = True
                self.write(self.run / 'input' / name, request)
                self.outer['Files'][name] = sha(self.encode(request))
                self.native['Files'] = copy.deepcopy(self.outer['Files'])
                self.write(self.run / 'request.json', self.outer)
                self.refresh()
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.reconcile()
            (self.run / 'input' / name).write_bytes(data)
            self.outer['Files'][name] = sha(data)
            self.native['Files'] = copy.deepcopy(self.outer['Files'])
            self.write(self.run / 'request.json', self.outer)
            self.refresh()
        for value in (True, 0, 1, 'false', None):
            self.artifact['formal_checks_verified'] = value
            self.refresh()
            with self.subTest(formal=value), self.assertRaises(ValueError):
                self.reconcile()
        self.assertEqual(self.store.calls, [])

    def test_real_isolated_store_saves_observation_without_advancing_parent(self):
        from tests.test_auxiliary_v14 import AuxiliaryTests
        from scripts.pair_auxiliary_runtime import saved_validator_inputs, _context
        from scripts.pair_audit_adapter import adapt_report
        from scripts.pair_core import PairError
        fixture = AuxiliaryTests()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        candidate = fixture.queue()
        ticket = fixture.reserve()
        fixture.start_fixture(ticket)
        with fixture.store.transaction() as db:
            db.execute("UPDATE auxiliary_tasks SET track='cloudflare' WHERE id='aux'")
            db.execute('UPDATE auxiliary_candidates SET tracks=?,coverage_units=? WHERE id=?',
                (json.dumps(['cloudflare']), json.dumps([{'id': 'source', 'track': 'cloudflare', 'required': True}]),
                 candidate['id']))
            execution = dict(fixture.store._auxiliary_execution(db, ticket['execution_id'], ticket['lease']))
            task = dict(fixture.store._auxiliary_task(db, execution['task_id']))
            fixed = dict(fixture.store._auxiliary_candidate(db, task['candidate_id']))
            context = _context(fixture.store, {'execution': execution, 'task': task, 'candidate': fixed})
            raw = {'context': context, 'verdict': 'needs_validation', 'findings': [], 'checks': [],
                   'coverage_ledger': [{'coverage_id': 'source', 'status': 'covered'}],
                   'coverage': [{'unit_id': 'source', 'status': 'reviewed', 'evidence_refs': ['fixture']}],
                   'unverified_candidates': []}
            adapted = adapt_report(raw, context=context, expected_units=[{'id': 'source', 'required': True}])
            fixture.store._save_auxiliary_report(db, execution, raw, adapted['adapted_report'],
                                                adapted['validation'], {'formal_checks_verified': False})
            before = dict(db.execute('SELECT * FROM review_stages WHERE id=?', (candidate['stage_id'],)).fetchone())
        self.prepare.side_effect = saved_validator_inputs
        self.prepared = saved_validator_inputs(fixture.store, *fixture.actor(), ticket['execution_id'], ticket['lease'])
        self.worker_request = validator_request(self.prepared, node_sha256=self.tools[host._TOOLS['node']].lower())
        for name in self.worker_request['files']:
            if name.endswith('.json') and name != 'report-schema.json':
                data = next(item['input_bytes'] for item in self.prepared['inputs']
                            if item['contract']['input_field'] + '.json' == name)
            else:
                data = (Path(__file__).resolve().parents[1] / 'assets/audit' / name).read_bytes()
            (self.run / 'input' / name).write_bytes(data)
            self.outer['Files'][name] = sha(data)
        self.write(self.run / 'input/request.json', self.worker_request)
        self.outer['Files']['request.json'] = sha(self.encode(self.worker_request))
        self.native['Files'] = copy.deepcopy(self.outer['Files'])
        self.write(self.run / 'request.json', self.outer)
        self.artifact['binding'] = self.worker_request['binding']
        self.artifact['files'] = self.worker_request['files']
        for result in self.artifact['results']:
            observed = self.artifact['reader_observations'][result['validator']][0]
            observed['source_sha256'] = self.worker_request['files']['WindowsSafeInput.cs']
            result['stderr'] = 'PAIR_READER_OBSERVATION:' + json.dumps(observed) + '\n'
            result['stderr_sha256'] = sha(result['stderr'].encode())
        self.refresh()
        args = (*fixture.actor(), ticket['execution_id'], ticket['lease'], self.run_id)
        saved = fixture.store.reconcile_validator_run(*args)
        self.assertTrue(saved['saved'])
        self.assertFalse(saved['formal_checks_verified'])
        self.assertTrue(fixture.store.reconcile_validator_run(*args)['duplicate'])
        with fixture.store.transaction() as db:
            record = fixture.store._option(db, 'validator_run:' + self.run_id)
            after = dict(db.execute('SELECT * FROM review_stages WHERE id=?', (candidate['stage_id'],)).fetchone())
        self.assertIn('HostObservation', record['native'])
        self.assertFalse(record['formal_checks_verified'])
        self.assertEqual(before, after)
        self.assertEqual(fixture.store.settle_audit(*fixture.actor(), candidate['id'])['verdict'], 'needs_validation')
        self.native['Native']['CPUSeconds'] = 0.75
        self.refresh()
        with self.assertRaisesRegex(PairError, 'receipt conflict'):
            fixture.store.reconcile_validator_run(*args)

    def test_bounded_independent_files_duplicate_fields_and_links(self):
        path = self.run / 'request.json'
        original = path.read_bytes()
        path.write_bytes(b' ' * 65537)
        with self.assertRaisesRegex(ValueError, 'bounded'):
            self.reconcile()
        path.write_bytes(b'{"Run":1,"Run":2}')
        with self.assertRaisesRegex(ValueError, 'Duplicate'):
            self.reconcile()
        path.write_bytes(original)
        target = self.root / 'original-request.json'
        path.rename(target)
        os.link(target, path)
        with self.assertRaisesRegex(ValueError, 'independent'):
            self.reconcile()
        path.unlink()
        try:
            path.symlink_to(target)
        except OSError:
            target.rename(path)
        else:
            with self.assertRaisesRegex(ValueError, 'Redirected'):
                self.reconcile()
        self.assertEqual(self.store.calls, [])

    def test_read_detects_identity_change(self):
        real = os.fstat
        count = 0
        def changed(fd):
            nonlocal count
            info = real(fd)
            count += 1
            if count == 2:
                values = list(info)
                values[6] += 1
                return os.stat_result(values)
            return info
        with patch.object(host.os, 'fstat', side_effect=changed):
            with self.assertRaisesRegex(ValueError, 'changed during read'):
                self.reconcile()
        self.assertEqual(self.store.calls, [])


class AdmittedHostTests(unittest.TestCase):
    def setUp(self):
        from tests.test_validator_admission import ValidatorAdmissionTests
        self.case = ValidatorAdmissionTests()
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        self.fixture = self.case.fixture
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        package = Path(__file__).resolve().parents[1]
        self.patch = patch.object(host, '_RUN_ROOT', self.root / 'runs')
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.package = package
        self.shell = Path(os.environ.get('SYSTEMROOT', r'C:\Windows')) / 'System32/WindowsPowerShell/v1.0/powershell.exe'

    def test_dispatch_uses_admission_before_external_process_and_preserves_fixed_dependency_bytes(self):
        from scripts.pair_core import FileLock
        from scripts.pair_validator_admission import verify_validator_admission
        from unittest.mock import Mock
        starts = []
        def start(argv, **options):
            self.assertEqual(options['env']['PSModulePath'], str(Path(argv[0]).parent / 'Modules'))
            run_id = argv[argv.index('-RunId') + 1]
            with self.fixture.store.transaction() as db:
                record = self.fixture.store._option(db, 'validator_admission:' + run_id)
            self.assertEqual(record['state'], 'dispatching')
            self.assertEqual(record['run_id'], run_id)
            # 控制锁覆盖本机控制器创建，等待进程时已释放。
            from scripts.pair_core import PairError
            with self.assertRaises(PairError):
                with FileLock(self.fixture.store.control_lock):
                    self.fail('Controller started outside admission boundary')
            directory = Path(argv[argv.index('-InputDirectory') + 1])
            for name in host._WORKER_SOURCES:
                self.assertEqual((directory / name).read_bytes(), (self.package / 'scripts' / name).read_bytes())
            self.assertEqual(json.loads((directory / 'request.json').read_bytes()), record['snapshot']['request'])
            starts.append((run_id, record))
            child = Mock(returncode=0)
            def completed(**kwargs):
                with FileLock(self.fixture.store.control_lock):
                    pass
                return b'', b''
            child.communicate.side_effect = completed
            return child
        with patch.object(host.subprocess, 'Popen', side_effect=start):
            result = host.dispatch_restricted_validator_run(self.fixture.store, *self.fixture.actor(),
                        self.case.ticket['execution_id'], self.case.ticket['lease'])
        self.assertEqual(len(starts), 1)
        self.assertTrue(result['dispatched'])
        self.assertFalse(result['formal_checks_verified'])
        permission = verify_validator_admission(self.fixture.store, *self.fixture.actor(),
            self.case.ticket['execution_id'], self.case.ticket['lease'], run_id=starts[0][0],
            tool_identity=starts[0][1]['snapshot']['tool_identity'], purpose='dispatch')
        self.assertFalse(permission['dispatch_allowed'])

    def test_pause_before_dispatch_starts_no_process(self):
        from scripts.pair_core import PairError
        self.fixture.store.request_pause(*self.fixture.actor(), reason='Host fixture pause', idempotency_key='host-pause')
        with patch.object(host.subprocess, 'Popen') as start, self.assertRaises(PairError):
            host.dispatch_restricted_validator_run(self.fixture.store, *self.fixture.actor(),
                self.case.ticket['execution_id'], self.case.ticket['lease'])
        start.assert_not_called()


if __name__ == '__main__':
    unittest.main()
