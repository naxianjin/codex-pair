"""使用已保存真实产物核对验收边界；不授予正式认证。"""
import base64
import copy
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.pair_validator_worker import verify_validator_result

run = Path(sys.argv[1])
evidence = json.loads((run / "native.json").read_text(encoding="utf-8-sig"))
request = json.loads((run / "input/request.json").read_text(encoding="utf-8-sig"))
artifact = json.loads(base64.b64decode(evidence["Native"]["ArtifactBase64"]))
result = verify_validator_result(request, evidence["Native"], artifact)
assert result["execution_completed"] and not result["formal_checks_verified"]
cases = []
for case in ("formal_claim", "binding_drift", "active_process", "timeout", "bool_exit", "wrong_diagnostic", "outcome_lie"):
    native = copy.deepcopy(evidence["Native"])
    changed = copy.deepcopy(artifact)
    if case == "formal_claim": changed["formal_checks_verified"] = True
    elif case == "binding_drift": changed["binding"] = {}
    elif case == "active_process": native["ActiveProcesses"] = 1
    elif case == "timeout": native["TimedOut"] = True
    elif case == "bool_exit": native["ExitCode"] = False
    elif case == "wrong_diagnostic": changed["results"][0]["stderr"] += "tampered"
    else: changed["validators_succeeded"] = not changed["validators_succeeded"]
    try: verify_validator_result(request, native, changed)
    except ValueError: cases.append({"case": case, "rejected": True})
    else: raise AssertionError(case + " accepted")
negative = copy.deepcopy(artifact)
negative["results"][0]["exit_code"] = 1
negative["validators_succeeded"] = False
business_failure = verify_validator_result(request, evidence["Native"], negative)
assert business_failure["execution_completed"] and not business_failure["validators_succeeded"]
assert not business_failure["formal_checks_verified"]
print(json.dumps({"business_failure_preserved": business_failure, "formal": False, "run": run.name, "positive": result, "cases": cases}, indent=2))
