"""检查受限树原始模块属性及程序集身份关联，不认证加载字节。"""
import collections
import json
import sys
from pathlib import Path
import xml.etree.ElementTree as ET


def check(data, *, profile="historical"):
    expected_pairs = {"historical": 46, "loaded-reader-v1": 52, "loaded-reader-v2": 52}[profile]
    ns = {"e": "http://schemas.microsoft.com/win/2004/08/events/event"}
    events = ET.fromstring(data["Trace"]["Xml"]).findall("e:Event", ns)
    starts = []
    for event in events:
        if event.find("e:System/e:Provider", ns).get("Name") == "Microsoft-Windows-Kernel-Process" and event.findtext("e:System/e:EventID", namespaces=ns) == "1":
            starts.append({x.get("Name"): (x.text or "").strip() for x in event.findall(".//e:EventData/e:Data", ns)})
    root = [x for x in starts if x["ProcessID"] == str(data["Native"]["RootPid"])]
    if not (len(root) == 1):
        raise ValueError("check-restricted-raw-modules.py: evidence invariant failed")
    tree = {(root[0]["ProcessID"], root[0]["ProcessSequenceNumber"])}
    for _ in starts:
        for item in starts:
            if (item["ParentProcessID"], item["ParentProcessSequenceNumber"]) in tree:
                tree.add((item["ProcessID"], item["ProcessSequenceNumber"]))
    if not (len(tree) == data["Native"]["TotalProcesses"] == 13):
        raise ValueError("check-restricted-raw-modules.py: evidence invariant failed")
    pids = {pid for pid, _ in tree}
    assemblies = set()
    for event in events:
        pid = event.find("e:System/e:Execution", ns).get("ProcessID")
        if pid in pids and event.find("e:System/e:Provider", ns).get("Name") == "Microsoft-Windows-DotNETRuntime" and event.findtext("e:System/e:EventID", namespaces=ns) == "154":
            fields = {x.tag.split("}")[-1]: x.text for x in event.findall("e:UserData/*/*", ns)}
            assemblies.add((pid, int(fields["AssemblyID"], 0), fields["ClrInstanceID"]))
    for template in data["Trace"]["ModuleTemplates"]:
        if not (type(template["Id"]) is int and type(template["Version"]) is int):
            raise ValueError("check-restricted-raw-modules.py: evidence invariant failed")
    templates = {(x["Id"], x["Version"]): [f.get("name") for f in ET.fromstring(x["Template"])] for x in data["Trace"]["ModuleTemplates"]}
    balance = collections.Counter()
    counts = collections.Counter()
    rows = []
    identities = {}
    for event in data["Trace"]["ModuleEvents"]:
        if not (type(event["Id"]) is int and type(event["Version"]) is int):
            raise ValueError("check-restricted-raw-modules.py: evidence invariant failed")
        if not (type(event["PID"]) is int):
            raise ValueError("check-restricted-raw-modules.py: evidence invariant failed")
        if str(event["PID"]) not in pids:
            continue
        names = templates[(event["Id"], event["Version"])]
        if not (len(names) == len(event["Properties"]) == 13):
            raise ValueError("check-restricted-raw-modules.py: evidence invariant failed")
        fields = dict(zip(names, event["Properties"]))
        if not (fields["Reserved1"] == "0" and fields["ClrInstanceID"].isdigit()):
            raise ValueError("check-restricted-raw-modules.py: evidence invariant failed")
        if not (not fields["ModuleILPath"].isdigit()):
            raise ValueError("check-restricted-raw-modules.py: evidence invariant failed")
        if not ((str(event["PID"]), int(fields["AssemblyID"]), fields["ClrInstanceID"]) in assemblies):
            raise ValueError("check-restricted-raw-modules.py: evidence invariant failed")
        key = (event["PID"], fields["ModuleID"], fields["AssemblyID"], fields["ClrInstanceID"])
        if not (key not in identities or identities[key] == fields):
            raise ValueError("check-restricted-raw-modules.py: evidence invariant failed")
        identities[key] = fields
        balance[key] += 1 if event["Id"] == 152 else -1
        counts[(key, event["Id"])] += 1
        rows.append({"event_id": event["Id"], "pid": event["PID"], "fields": fields})
    if not (len(balance) == expected_pairs and all(v == 0 for v in balance.values())):
        raise ValueError("check-restricted-raw-modules.py: evidence invariant failed")
    if not (len(counts) == expected_pairs * 2 and all(v == 1 for v in counts.values())):
        raise ValueError("check-restricted-raw-modules.py: evidence invariant failed")
    return {"formal": False, "tree_processes": len(tree), "module_pairs": len(balance), "modules": rows}


if __name__ == "__main__":
    data = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8-sig"))
    print(json.dumps(check(data, profile=sys.argv[2] if len(sys.argv) == 3 else "historical"), indent=2))
