"""检查受限树原始模块属性及程序集身份关联，不认证加载字节。"""
import collections
import json
import sys
from pathlib import Path
import xml.etree.ElementTree as ET


def check(data):
    ns = {"e": "http://schemas.microsoft.com/win/2004/08/events/event"}
    events = ET.fromstring(data["Trace"]["Xml"]).findall("e:Event", ns)
    starts = []
    for event in events:
        if event.find("e:System/e:Provider", ns).get("Name") == "Microsoft-Windows-Kernel-Process" and event.findtext("e:System/e:EventID", namespaces=ns) == "1":
            starts.append({x.get("Name"): (x.text or "").strip() for x in event.findall(".//e:EventData/e:Data", ns)})
    root = [x for x in starts if x["ProcessID"] == str(data["Native"]["RootPid"])]
    assert len(root) == 1
    tree = {(root[0]["ProcessID"], root[0]["ProcessSequenceNumber"])}
    for _ in starts:
        for item in starts:
            if (item["ParentProcessID"], item["ParentProcessSequenceNumber"]) in tree:
                tree.add((item["ProcessID"], item["ProcessSequenceNumber"]))
    assert len(tree) == data["Native"]["TotalProcesses"] == 13
    pids = {pid for pid, _ in tree}
    assemblies = set()
    for event in events:
        pid = event.find("e:System/e:Execution", ns).get("ProcessID")
        if pid in pids and event.find("e:System/e:Provider", ns).get("Name") == "Microsoft-Windows-DotNETRuntime" and event.findtext("e:System/e:EventID", namespaces=ns) == "154":
            fields = {x.tag.split("}")[-1]: x.text for x in event.findall("e:UserData/*/*", ns)}
            assemblies.add((pid, int(fields["AssemblyID"], 0), fields["ClrInstanceID"]))
    templates = {(x["Id"], x["Version"]): [f.get("name") for f in ET.fromstring(x["Template"])] for x in data["Trace"]["ModuleTemplates"]}
    balance = collections.Counter()
    counts = collections.Counter()
    rows = []
    for event in data["Trace"]["ModuleEvents"]:
        if str(event["PID"]) not in pids:
            continue
        names = templates[(event["Id"], event["Version"])]
        assert len(names) == len(event["Properties"]) == 13
        fields = dict(zip(names, event["Properties"]))
        assert fields["Reserved1"] == "0" and fields["ClrInstanceID"].isdigit()
        assert not fields["ModuleILPath"].isdigit()
        assert (str(event["PID"]), int(fields["AssemblyID"]), fields["ClrInstanceID"]) in assemblies
        key = (event["PID"], fields["ModuleID"], fields["AssemblyID"], fields["ClrInstanceID"])
        balance[key] += 1 if event["Id"] == 152 else -1
        counts[(key, event["Id"])] += 1
        rows.append({"event_id": event["Id"], "pid": event["PID"], "fields": fields})
    assert len(balance) == 46 and all(v == 0 for v in balance.values())
    assert len(counts) == 92 and all(v == 1 for v in counts.values())
    return {"formal": False, "tree_processes": len(tree), "module_pairs": len(balance), "modules": rows}


if __name__ == "__main__":
    data = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8-sig"))
    print(json.dumps(check(data), indent=2))
