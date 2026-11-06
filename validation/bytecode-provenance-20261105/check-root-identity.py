"""用原生句柄身份核对ETW；仅为离线诊断检查。"""
import copy
import datetime
import json
from pathlib import Path
import re
import xml.etree.ElementTree as ET


def filetime(value):
    whole, fraction = value.rstrip('Z').split('.')
    delta = datetime.datetime.fromisoformat(whole) - datetime.datetime(1601, 1, 1)
    return (delta.days * 86400 + delta.seconds) * 10000000 + int(fraction[:7])


def check(data):
    ns = {'e': 'http://schemas.microsoft.com/win/2004/08/events/event'}
    native = data['Native']
    assert re.search(r'Total Events\s+Lost\s+0\s', data['Trace']['Summary'])
    starts = []
    for event in ET.fromstring(data['Trace']['Xml']).findall('e:Event', ns):
        if event.findtext('e:System/e:EventID', namespaces=ns) == '1':
            starts.append({item.attrib.get('Name'): (item.text or '').strip()
                           for item in event.findall('.//e:EventData/e:Data', ns)})
    matches = [item for item in starts if item['ProcessID'] == str(native['RootPid'])
               and filetime(item['CreateTime']) == native['RootCreationFileTime']]
    assert len(matches) == 1
    assert native['ExitCode'] == native['ActiveProcesses'] == 0
    assert native['TimedOut'] is False and data['Trace']['Formal'] is False
    return matches[0]


if __name__ == '__main__':
    root = Path(__file__).resolve().parent
    data = json.loads((root / 'root-identity-evidence.json').read_text())
    match = check(data)
    for field in ('RootPid', 'RootCreationFileTime'):
        changed = copy.deepcopy(data)
        changed['Native'][field] += 1
        try:
            check(changed)
        except AssertionError:
            continue
        raise AssertionError('Changed identity accepted: ' + field)
    print('Native PID and 100ns creation time match one ETW start; two negative cases rejected; formal=false')
