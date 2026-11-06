"""按同次客体模板解码原始CLR属性；不认证加载内容。"""
import collections
import copy
import json
from pathlib import Path
import xml.etree.ElementTree as ET


def check(data):
    templates = {(int(t["Id"]), int(t["Version"])): [x.get("name") for x in ET.fromstring(t["Template"])] for t in data["Templates"]}
    balance = collections.Counter()
    decoded = []
    for event in data["Events"]:
        names = templates[(event["Id"], event["Version"])]
        assert len(names) == len(event["Properties"]) == 13
        fields = dict(zip(names, event["Properties"]))
        assert fields["Reserved1"] == "0"
        assert fields["ClrInstanceID"].isdigit()
        assert not fields["ModuleILPath"].isdigit()
        key = (event["PID"], fields["ModuleID"], fields["AssemblyID"], fields["ClrInstanceID"])
        balance[key] += 1 if event["Id"] == 152 else -1
        decoded.append({"event_id": event["Id"], "pid": event["PID"], "fields": fields})
    assert data["ChildExit"] == 0 and len(balance) == 21 and all(v == 0 for v in balance.values())
    return decoded


if __name__ == "__main__":
    root = Path(__file__).resolve().parent
    data = json.loads((root / "guest-clr-raw-properties.json").read_text(encoding="utf-8-sig"))
    decoded = check(data)
    for case in ("missing_property", "wrong_version", "missing_unload"):
        changed = copy.deepcopy(data)
        if case == "missing_property": changed["Events"][0]["Properties"].pop(3)
        elif case == "wrong_version": changed["Events"][0]["Version"] = 999
        else: changed["Events"].pop(next(i for i,e in enumerate(changed["Events"]) if e["Id"] == 153))
        try: check(changed)
        except (AssertionError, KeyError): pass
        else: raise AssertionError(case + " accepted")
    print(json.dumps({"formal": False, "decoded_events": len(decoded), "module_pairs": 21, "negative_cases": 3, "modules": decoded}, indent=2))
