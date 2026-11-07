"""将系统映像观察绑定到同次受限子树；不提供正式宿主认证。"""
import argparse
import datetime
import importlib.util
import json
import ntpath
from pathlib import Path
import re
import xml.etree.ElementTree as ET


def check_process_binding(native, capture):
    terminal = native['Native']
    if (native.get('Formal') is not False or native['Trace'].get('Formal') is not False
            or type(terminal.get('ExitCode')) is not int or terminal['ExitCode'] != 0
            or type(terminal.get('ActiveProcesses')) is not int or terminal['ActiveProcesses'] != 0
            or terminal.get('TimedOut') is not False
            or type(terminal.get('TotalProcesses')) is not int or terminal['TotalProcesses'] != 3):
        raise ValueError('Restricted image process terminal state mismatch')
    losses = re.findall(r'(?m)^Total Events[^\S\r\n]+Lost[^\S\r\n]+(\d+)[ \t\r]*$', native['Trace']['Summary'])
    if losses != ['0']:
        raise ValueError('Restricted image ETW loss count missing or nonzero')
    ns = {'e': 'http://schemas.microsoft.com/win/2004/08/events/event'}
    rows = []
    for event in ET.fromstring(native['Trace']['Xml']).findall('e:Event', ns):
        provider = event.find('e:System/e:Provider', ns)
        if provider.get('Guid', '').lower() != '{22fb2cd6-0e7b-422b-a0c7-2fad1fd0e716}':
            continue
        fields = {item.get('Name'): (item.text or '').strip()
                  for item in event.findall('e:EventData/e:Data', ns)}
        if fields.get('EventsLost', '0') != '0':
            raise ValueError('Restricted image XML reports lost events')
        rows.append((int(event.findtext('e:System/e:EventID', namespaces=ns)), fields))
    starts = [fields for event_id, fields in rows if event_id == 1]
    roots = [item for item in starts if item['ProcessID'] == str(terminal['RootPid'])
             and _filetime(item['CreateTime']) == terminal['RootCreationFileTime']]
    if len(roots) != 1:
        raise ValueError('Restricted image native root identity mismatch')
    tree = {(roots[0]['ProcessID'], roots[0]['ProcessSequenceNumber'])}
    for _ in starts:
        for item in starts:
            if (item['ParentProcessID'], item['ParentProcessSequenceNumber']) in tree:
                tree.add((item['ProcessID'], item['ProcessSequenceNumber']))
    if len(tree) != 3:
        raise ValueError('Restricted image process coverage mismatch')
    for pid, sequence in tree:
        exits = [fields for event_id, fields in rows if event_id == 2
                 and fields['ProcessID'] == pid and fields['ProcessSequenceNumber'] == sequence]
        if len(exits) != 1 or exits[0]['ExitCode'] != '0':
            raise ValueError('Restricted image child exit missing or failed')
    matches = [item for item in starts if (item['ProcessID'], item['ProcessSequenceNumber']) in tree
               and item['ProcessID'] == str(capture['PID'])
               and _filetime(item['CreateTime']) == _filetime(capture['ProcessCreationUTC'])
               and ntpath.basename(item['ImageName']).lower() == 'powershell.exe'
               and item['ParentProcessID'] == roots[0]['ProcessID']]
    if len(matches) != 1:
        raise ValueError('System image capture is not the restricted PowerShell child')
    image_rows = []
    for image in capture['Images']:
        loads = [fields for event_id, fields in rows if event_id == 5
                 and fields.get('ProcessID') == str(capture['PID'])
                 and ntpath.basename(fields['ImageName']).lower() == image['Name'].lower()]
        if (len(loads) != 1 or int(loads[0]['ImageBase'], 0) != int(image['BaseAddress'])
                or int(loads[0]['ImageSize'], 0) != image['ImageSize']):
            raise ValueError('System image ETW load identity mismatch')
        image_rows.append({'image': image['Name'].lower(), 'base_address': image['BaseAddress'],
                           'image_size': image['ImageSize']})
    return {'formal': False, 'processes': 3, 'capture_pid': capture['PID'],
            'process_binding_verified': True, 'images': image_rows}


def _filetime(value):
    match = re.fullmatch(r'(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)\.(\d{7,9})Z', value)
    if not match:
        raise ValueError('Invalid captured process creation timestamp')
    delta = datetime.datetime.fromisoformat(match[1]) - datetime.datetime(1601, 1, 1)
    return (delta.days * 86400 + delta.seconds) * 10000000 + int(match[2][:7])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('native', 'capture', 'bytes-dir', 'contract', 'collector-sha256'):
        parser.add_argument('--' + name, required=True)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    spec = importlib.util.spec_from_file_location('system_image_consistency',
        Path(__file__).with_name('check-system-image-consistency.py'))
    checker = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(checker)
    native_path = Path(args.native)
    if native_path.stat().st_size > 8 * 1024 * 1024:
        raise ValueError('Restricted image native evidence exceeded limit')
    native = checker._read_json(native_path)
    capture = checker._read_json(Path(args.capture))
    process = check_process_binding(native, capture)
    contract_path = Path(args.contract)
    contract_bytes = checker._read_bytes(contract_path, contract_path.stat().st_size)
    result = checker.check(capture, args.bytes_dir, checker._json_bytes(contract_bytes),
        collector_sha256=args.collector_sha256, contract_sha256=checker._sha(contract_bytes))
    result['restricted_process_observation'] = process
    text = json.dumps(result, ensure_ascii=False, indent=2) + '\n'
    if args.output:
        args.output.write_text(text, encoding='utf-8')
    print(text, end='')


if __name__ == '__main__':
    main()
