"""在 Downloads 新目录准备持久 Store 夹具，并对账真实受限校验运行。"""
import argparse
import copy
import hashlib
import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from scripts.install_skill import package_manifest
from scripts.pair_auxiliary import upgrade_auxiliary_v14
from scripts.pair_auxiliary_runtime import _context, saved_validator_inputs
from scripts.pair_audit_adapter import adapt_report
from scripts.pair_core import PairError, Store
from scripts.pair_validator_worker import validator_request
from tests.test_auxiliary_v14 import AuxiliaryTests


SESSIONS = {'A': '00000000-0000-0000-0000-000000000001',
            'B': '00000000-0000-0000-0000-000000000002'}
DOWNLOADS = Path.home() / 'Downloads'
COVERAGE_ID = 'source::input::validator::integrity'
COVERAGE_LEDGER = [{
    'coverage_id': COVERAGE_ID,
    'canonical_refs': {'surface': 'source', 'boundary': 'input',
                       'subsystem': 'validator', 'attack_class': 'integrity'},
    'surface': 'source', 'boundary': 'input', 'subsystem': 'validator',
    'attack_class': 'integrity', 'starting_paths': ['app.py'],
    'ordinary_attack_class_block': None, 'selected_companion_blocks': [],
    'excluded_blocks': [], 'prior_status': 'new', 'attempts': [], 'wave': 1,
    'status': 'covered', 'agent_id': 'fixture', 'reviewed_paths': ['app.py'],
    'local_checks': [{'agent_id': 'fixture', 'reviewed_paths': ['app.py'],
                      'invariant': 'Synthetic Store fixture only', 'method': 'source',
                      'result': 'Fixed local diagnostic fixture', 'artifact': None}],
    'result_fingerprints': [], 'unresolved': []}]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('prepare', 'reconcile'))
    parser.add_argument('directory', type=Path)
    parser.add_argument('--run-id')
    args = parser.parse_args()
    directory = args.directory.absolute()
    if (directory.resolve() != directory or directory == DOWNLOADS.resolve()
            or not directory.is_relative_to(DOWNLOADS.resolve())):
        raise ValueError('Diagnostic fixture must be a plain directory below Downloads')
    if args.action == 'prepare':
        if args.run_id is not None:
            raise ValueError('Prepare does not accept a prior run')
        result = prepare(directory)
    else:
        if args.run_id is None:
            raise ValueError('Reconcile requires an existing run ID')
        result = reconcile(directory, args.run_id)
    print(json.dumps(result, ensure_ascii=True, sort_keys=True))


def prepare(directory):
    package_manifest(ROOT)
    directory.mkdir(parents=False, exist_ok=False)
    fixture = AuxiliaryTests()
    fixture.root = directory
    fixture.project = directory / 'project'
    fixture.project.mkdir()
    fixture.home = directory / 'codex-home'
    fixture.home.mkdir()
    fixture.store = Store.initialize('test', str(fixture.project), 'a' * 40, home=fixture.home)
    fixture.sessions = dict(SESSIONS)
    fixture.directories = {}
    for member in ('A', 'B'):
        cwd = directory / member
        cwd.mkdir()
        fixture.directories[member] = str(cwd)
        fixture.store.join(member, SESSIONS[member], str(cwd), backend='queue', owned=False,
                           delivery_mode='manual', proof={'id': SESSIONS[member], 'cwd': str(cwd)})
    with fixture.store.transaction() as db:
        upgrade_auxiliary_v14(db)
        for member in ('A', 'B'):
            proof = {'id': SESSIONS[member], 'cwd': fixture.directories[member],
                     'modelProvider': 'openai', 'model': 'gpt-6.1-sol', 'fixture_only': True}
            db.execute('UPDATE members SET proof=?,verified=1,online=1 WHERE id=?',
                       (json.dumps(proof), member))
    fixture.store.auxiliary_policy(*fixture.actor(), enabled=True, reason='隔离诊断夹具，无真实模型身份')
    fixture.store.allow_auxiliary_config(*fixture.actor(), 'allowed-a', 'A', '隔离诊断夹具配置')
    fixture._index = 10
    candidate = fixture.queue()
    ticket = fixture.reserve()
    fixture.start_fixture(ticket)
    with fixture.store.transaction() as db:
        db.execute("UPDATE auxiliary_tasks SET track='cloudflare',unit_ids=? WHERE id='aux'",
                   (json.dumps([COVERAGE_ID]),))
        db.execute('UPDATE auxiliary_candidates SET tracks=?,coverage_units=? WHERE id=?',
                   (json.dumps(['cloudflare']),
                    json.dumps([{'id': COVERAGE_ID, 'track': 'cloudflare', 'required': True}]), candidate['id']))
        execution = dict(fixture.store._auxiliary_execution(db, ticket['execution_id'], ticket['lease']))
        task = dict(fixture.store._auxiliary_task(db, execution['task_id']))
        fixed = dict(fixture.store._auxiliary_candidate(db, task['candidate_id']))
        context = _context(fixture.store, {'execution': execution, 'task': task, 'candidate': fixed})
        raw = {'context': context, 'verdict': 'needs_validation', 'findings': [], 'checks': [],
               'coverage_ledger': COVERAGE_LEDGER,
               'coverage': [{'unit_id': COVERAGE_ID, 'status': 'reviewed', 'evidence_refs': ['fixture']}],
               'unverified_candidates': []}
        adapted = adapt_report(raw, context=context, expected_units=[{'id': COVERAGE_ID, 'required': True}])
        fixture.store._save_auxiliary_report(db, execution, raw, adapted['adapted_report'],
                                            adapted['validation'], {'formal_checks_verified': False,
                                                                    'fixture_only': True})
        stage = dict(db.execute('SELECT * FROM review_stages WHERE id=?', (candidate['stage_id'],)).fetchone())
    prepared = saved_validator_inputs(fixture.store, *fixture.actor(), ticket['execution_id'], ticket['lease'])
    baseline = json.loads((Path(__file__).parent / 'root-identity-baseline.json').read_bytes())
    request = validator_request(prepared, node_sha256=baseline['tools'][r'C:\PairLab\Tools\node\node.exe'].lower())
    inputs = directory / 'input'
    inputs.mkdir()
    for item in prepared['inputs']:
        (inputs / (item['contract']['input_field'] + '.json')).write_bytes(item['input_bytes'])
    for name, digest in request['files'].items():
        if not (inputs / name).exists():
            (inputs / name).write_bytes((ROOT / 'assets/audit' / name).read_bytes())
        if hashlib.sha256((inputs / name).read_bytes()).hexdigest() != digest:
            raise ValueError('Diagnostic input digest mismatch')
    (inputs / 'pair_validator_worker.py').write_bytes((ROOT / 'scripts/pair_validator_worker.py').read_bytes())
    write_new(inputs / 'request.json', request)
    write_new(directory / 'fixture.json', {'schema_version': 1, 'formal': False, 'fixture_only': True,
              'execution_id': ticket['execution_id'], 'lease': ticket['lease'],
              'candidate_id': candidate['id'], 'stage_id': candidate['stage_id'],
              'stage_before': stage, 'request_sha256': hashlib.sha256((inputs / 'request.json').read_bytes()).hexdigest()})
    return {'formal': False, 'fixture_only': True, 'directory': str(directory),
            'input_directory': str(inputs), 'state_file': str(fixture.store.path),
            'execution_id': ticket['execution_id'], 'status': 'prepared',
            'limit': 'Synthetic model/session boundary; real Store and fixed report bytes only.'}


def reconcile(directory, run_id):
    metadata = json.loads((directory / 'fixture.json').read_bytes())
    if (metadata.get('schema_version') != 1 or metadata.get('formal') is not False
            or metadata.get('fixture_only') is not True):
        raise ValueError('Diagnostic fixture marker required')
    store = Store('test', home=directory / 'codex-home')
    actor = ('A', SESSIONS['A'], str(directory / 'A'))
    args = (*actor, metadata['execution_id'], metadata['lease'], run_id)
    result = store.reconcile_validator_run(*args)
    duplicate = store.reconcile_validator_run(*args)
    if (result['formal_checks_verified'] is not False or result['validators_succeeded'] is not True
            or duplicate['duplicate'] is not True):
        raise ValueError('Diagnostic status or idempotency changed')
    with store.transaction() as db:
        record = store._option(db, 'validator_run:' + run_id)
        stage = dict(db.execute('SELECT * FROM review_stages WHERE id=?', (metadata['stage_id'],)).fetchone())
    if stage != metadata['stage_before']:
        raise ValueError('Diagnostic run unexpectedly changed parent stage')
    conflict = copy.deepcopy(record['native'])
    conflict['HostObservation']['native_sha256'] = '0' * 64
    try:
        store.record_validator_run(*actor, metadata['execution_id'], metadata['lease'], run_id,
                                   record['request'], conflict, record['artifact'], record['tool_identity'])
    except PairError as error:
        if 'receipt conflict' not in str(error):
            raise
    else:
        raise ValueError('Conflicting diagnostic run accepted')
    settlement = store.settle_audit(*actor, metadata['candidate_id'])
    if settlement['verdict'] != 'needs_validation':
        raise ValueError('Diagnostic result unexpectedly passed parent audit')
    with store.transaction() as db:
        stage = dict(db.execute('SELECT * FROM review_stages WHERE id=?', (metadata['stage_id'],)).fetchone())
    if stage != metadata['stage_before']:
        raise ValueError('Diagnostic settlement unexpectedly changed parent stage')
    summary = {'formal': False, 'fixture_only': True, 'run_id': run_id, 'saved': result['saved'],
               'validators_succeeded': result['validators_succeeded'], 'duplicate_verified': True,
               'conflict_rejected': True, 'parent_stage_unchanged': True,
               'parent_verdict': settlement['verdict'], 'remaining_checks': result['remaining_checks']}
    write_new(directory / ('reconciled-' + run_id + '.json'), summary)
    return summary


def write_new(path, value):
    with path.open('x', encoding='utf-8', newline='\n') as stream:
        json.dump(value, stream, ensure_ascii=False, sort_keys=True, indent=2)
        stream.write('\n')


if __name__ == '__main__':
    main()
