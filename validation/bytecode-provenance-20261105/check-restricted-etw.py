"""复核本次诊断证据，不授予正式隔离认证。"""
import copy
import json
from pathlib import Path
import re
import xml.etree.ElementTree as ET


def check(rows, summary, lost):
    assert lost == 0
    starts = [row['fields'] for row in rows if row['event_id'] == 1]
    root = summary['tree'][0]
    tree = {(root['ProcessID'], root['ProcessSequenceNumber'])}
    for _ in starts:
        for item in starts:
            if (item['ParentProcessID'], item['ParentProcessSequenceNumber']) in tree:
                tree.add((item['ProcessID'], item['ProcessSequenceNumber']))
    assert len(tree) == summary['native']['TotalProcesses'] == 13
    for pid, sequence in tree:
        assert sum(row['event_id'] == 2 and row['fields'].get('ProcessID') == pid
                   and row['fields'].get('ProcessSequenceNumber') == sequence for row in rows) == 1
    assert summary['native']['ExitCode'] == summary['native']['ActiveProcesses'] == 0
    assert summary['native']['TimedOut'] is False and summary['formal'] is False


if __name__ == '__main__':
    root = Path(__file__).resolve().parent
    ns = {'e': 'http://schemas.microsoft.com/win/2004/08/events/event'}
    rows = []
    for event in ET.parse(root / 'restricted-etw-events.xml').getroot().findall('e:Event', ns):
        rows.append({'event_id': int(event.findtext('e:System/e:EventID', namespaces=ns)),
                     'fields': {item.attrib.get('Name'): (item.text or '').strip()
                                for item in event.findall('.//e:EventData/e:Data', ns)}})
    summary = json.loads((root / 'restricted-etw-summary.json').read_text())
    lost = int(re.search(r'Total Events\s+Lost\s+(\d+)',
                         (root / 'restricted-etw-tracerpt.txt').read_text()).group(1))
    check(rows, summary, lost)
    for kind in ('missing_exit', 'wrong_pid', 'wrong_sequence', 'lost_event'):
        changed = copy.deepcopy(rows)
        target = next(row for row in changed if row['event_id'] == 2
                      and row['fields'].get('ProcessID') == summary['tree'][0]['ProcessID'])
        if kind == 'missing_exit':
            changed.remove(target)
        elif kind == 'wrong_pid':
            target['fields']['ProcessID'] = '999999'
        elif kind == 'wrong_sequence':
            target['fields']['ProcessSequenceNumber'] = '999999'
        try:
            check(changed, summary, 1 if kind == 'lost_event' else lost)
        except AssertionError:
            continue
        raise AssertionError('Negative evidence accepted: ' + kind)
    print('13 process lifecycles verified; four negative cases rejected; formal=false')
