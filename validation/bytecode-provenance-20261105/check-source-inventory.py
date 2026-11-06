"""复核诊断源码记录；这不是独立可信能力或运行时门禁。"""
import argparse
import hashlib
import json
from pathlib import Path


def check(expected, actual):
    if expected != actual:
        raise ValueError('Diagnostic source inventory mismatch')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-directory', type=Path, default=Path(__file__).resolve().parent)
    root = parser.parse_args().source_directory
    inventory = json.loads((root / 'root-gate-source-inventory.json').read_text())
    expected = inventory['files']
    actual = {name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in expected}
    check(expected, actual)
    for name in expected:
        changed = dict(actual)
        changed[name] = '0' * 64
        try:
            check(expected, changed)
        except ValueError:
            continue
        raise AssertionError('Changed source accepted: ' + name)
    receipt = json.loads((root / 'final-gate-live-receipt.json').read_text())
    assert receipt['ControllerSHA256'].lower() == actual['Invoke-ValidatorRootDiagnostic.ps1']
    assert inventory['formal'] is False and receipt['Formal'] is False
    print('Five source hashes and controller receipt match; five mismatches rejected; formal=false')
