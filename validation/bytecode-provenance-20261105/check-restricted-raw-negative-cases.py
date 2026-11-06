"""对已保存真实证据执行模块关联破坏测试，不运行客体。"""
import copy
import importlib.util
import json
from pathlib import Path

root = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("raw", root / "check-restricted-raw-modules.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
data = json.loads((root / "restricted-raw-module-evidence.json").read_text(encoding="utf-8-sig"))
result = module.check(data)
pids = {item["pid"] for item in result["modules"]}
index = next(i for i,e in enumerate(data["Trace"]["ModuleEvents"]) if e["PID"] in pids)
cases = []
for case in ("missing_property", "wrong_assembly", "wrong_clr", "missing_event", "duplicate_complete_pair", "unknown_version"):
    changed = copy.deepcopy(data)
    event = changed["Trace"]["ModuleEvents"][index]
    if case == "missing_property": event["Properties"].pop(3)
    elif case == "wrong_assembly": event["Properties"][1] = "0"
    elif case == "wrong_clr": event["Properties"][6] = "9999"
    elif case == "missing_event": changed["Trace"]["ModuleEvents"].pop(index)
    elif case == "unknown_version": event["Version"] = 999
    else:
        changed["Trace"]["ModuleEvents"] += copy.deepcopy([e for e in data["Trace"]["ModuleEvents"] if e["PID"] == event["PID"] and e["Properties"][0] == event["Properties"][0]])
    try: module.check(changed)
    except (AssertionError, KeyError): cases.append({"case": case, "rejected": True})
    else: raise AssertionError(case + " accepted")
print(json.dumps({"formal": False, "positive_pairs": result["module_pairs"], "cases": cases}, indent=2))
