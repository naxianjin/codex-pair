"""复算受限树观察摘要，不授予运行时或正式认证。"""
import collections
import copy
import json
import sys
from pathlib import Path
import xml.etree.ElementTree as ET


def check(data, *, profile="historical"):
    expected_pairs, expected_images = {"historical": (46, 106), "loaded-reader-v1": (52, 110)}[profile]
    ns = {'e': 'http://schemas.microsoft.com/win/2004/08/events/event'}
    rows = []
    for event in ET.fromstring(data['Trace']['Xml']).findall('e:Event', ns):
        rows.append((event.find('e:System/e:Provider', ns).attrib.get('Name'),
                     int(event.findtext('e:System/e:EventID', namespaces=ns)),
                     event.find('e:System/e:Execution', ns).attrib['ProcessID'],
                     {x.attrib.get('Name'): (x.text or '').strip() for x in event.findall('.//e:EventData/e:Data', ns)},
                     {x.tag.split('}')[-1]: x.text or '' for x in event.findall('e:UserData/*/*', ns)}))
    starts = [r[3] for r in rows if r[0] == 'Microsoft-Windows-Kernel-Process' and r[1] == 1]
    roots = [r for r in starts if r['ProcessID'] == str(data['Native']['RootPid'])]
    if not (len(roots) == 1):
        raise ValueError("check-combined-evidence.py: evidence invariant failed")
    root = roots[0]
    tree = {(root['ProcessID'], root['ProcessSequenceNumber'])}
    for _ in starts:
        for item in starts:
            if (item['ParentProcessID'], item['ParentProcessSequenceNumber']) in tree:
                tree.add((item['ProcessID'], item['ProcessSequenceNumber']))
    if not (len(tree) == data['Native']['TotalProcesses'] == 13):
        raise ValueError("check-combined-evidence.py: evidence invariant failed")
    pids = {pid for pid, _ in tree}
    balance = collections.Counter()
    for provider, event_id, pid, fields, managed in rows:
        if provider == 'Microsoft-Windows-DotNETRuntime' and pid in pids and event_id in (154, 155):
            key = (pid, managed['ClrInstanceID'], managed['AppDomainID'],
                   managed['AssemblyID'], managed['FullyQualifiedAssemblyName'])
            balance[key] += 1 if event_id == 154 else -1
    if not (len(balance) == expected_pairs and all(value == 0 for value in balance.values())):
        raise ValueError("check-combined-evidence.py: evidence invariant failed")
    images = {r[3]['ImageName'] for r in rows if r[0] == 'Microsoft-Windows-Kernel-Process'
              and r[1] == 5 and r[3].get('ProcessID') in pids}
    hashes = {item['Image']: item for item in data['Trace']['Images']}
    if not (len(images) == expected_images):
        raise ValueError("check-combined-evidence.py: evidence invariant failed")
    if not (all(name in hashes and len(hashes[name].get('SHA256', '')) == 64 for name in images)):
        raise ValueError("check-combined-evidence.py: evidence invariant failed")
    return len(tree), len(balance), len(images)


if __name__ == '__main__':
    root = Path(__file__).resolve().parent
    profile = sys.argv[2] if len(sys.argv) == 3 else 'historical'
    data = json.loads(Path(sys.argv[1]).read_text(encoding='utf-8-sig')) if len(sys.argv) >= 2 else json.loads((root / 'restricted-combined-evidence.json').read_text())
    checked = check(data, profile=profile)
    changed = copy.deepcopy(data)
    changed['Trace']['Images'] = []
    try:
        check(changed, profile=profile)
    except ValueError:
        pass
    else:
        raise AssertionError('Missing image hashes accepted')
    print(json.dumps({'formal': False, 'profile': profile, 'processes': checked[0], 'assembly_pairs': checked[1], 'image_hashes': checked[2], 'missing_hashes_rejected': True}))
