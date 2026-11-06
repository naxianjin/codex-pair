"""针对真实组合证据逐项破坏，检查诊断门槛拒绝缺失与错配。"""
import copy
import importlib.util
import json
from pathlib import Path
import xml.etree.ElementTree as ET

root = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("combined", root / "check-combined-evidence.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
data = json.loads((root / "combined-gate-live-evidence.json").read_text(encoding="utf-8"))
assert module.check(data) == (13, 46, 106)
ns = {"e": "http://schemas.microsoft.com/win/2004/08/events/event"}
results = []
for case in ("missing_unload", "wrong_clr", "wrong_assembly", "missing_hashes", "short_hash", "wrong_root"):
    changed = copy.deepcopy(data)
    if case in ("missing_unload", "wrong_clr", "wrong_assembly"):
        xml = ET.fromstring(changed["Trace"]["Xml"])
        event = next(e for e in xml.findall("e:Event", ns)
                     if e.findtext("e:System/e:EventID", namespaces=ns) == "155"
                     and e.find("e:System/e:Provider", ns).get("Name") == "Microsoft-Windows-DotNETRuntime")
        if case == "missing_unload":
            xml.remove(event)
        else:
            field = "ClrInstanceID" if case == "wrong_clr" else "AssemblyID"
            item = next(x for x in event.findall("e:UserData/*/*", ns) if x.tag.split("}")[-1] == field)
            item.text = "999999999"
        changed["Trace"]["Xml"] = ET.tostring(xml, encoding="unicode")
    elif case == "missing_hashes":
        changed["Trace"]["Images"] = []
    elif case == "short_hash":
        for item in changed["Trace"]["Images"]:
            item["SHA256"] = "0"
    else:
        changed["Native"]["RootPid"] = -1
    try:
        module.check(changed)
    except AssertionError:
        results.append({"case": case, "rejected": True})
    else:
        raise AssertionError(case + " accepted")
print(json.dumps({"positive": True, "negative_cases": results, "formal": False}, indent=2))
