"""核对观察到的NativeImage与已签名IL路径关系，不认证加载字节。"""
import copy
import json
from pathlib import Path


def check(modules, images, il_files):
    il = {x["Path"].casefold(): x for x in il_files}
    unsigned = {x["Path"].casefold(): x for x in images if x["Status"] == "NotSigned"}
    assert len(unsigned) == 15
    links = {}
    dynamic = set()
    for row in modules:
        if row["event_id"] != 152:
            continue
        fields = row["fields"]
        source = fields["ModuleILPath"]
        if len(source) < 3 or source[1:3] != ":\\":
            dynamic.add(source)
            continue
        assert source.casefold() in il and il[source.casefold()]["Status"] == "Valid"
        native = fields["ModuleNativePath"].casefold()
        if native in unsigned:
            link = {"native": unsigned[native]["Path"], "native_sha256": unsigned[native]["SHA256"],
                    "il": source, "il_sha256": il[source.casefold()]["SHA256"]}
            assert native not in links or links[native] == link
            links[native] = link
    assert set(links) == set(unsigned)
    assert len(dynamic) == 3
    return {"formal": False, "native_il_links": list(links.values()), "dynamic_names": sorted(dynamic),
            "limitation": "Separate post-run disk signature observations and ETW path association; no native/IL content equivalence, trusted baseline or loaded-byte proof."}


if __name__ == "__main__":
    root = Path(__file__).resolve().parent
    modules = json.loads((root / "restricted-raw-modules-results.json").read_text())["modules"]
    images = json.loads((root / "guest-all-image-signatures.json").read_text(encoding="utf-8-sig"))["Files"]
    il = json.loads((root / "guest-il-content-observations.json").read_text(encoding="utf-8-sig"))["Files"]
    result = check(modules, images, il)
    for case in ("missing_il", "invalid_signature", "missing_native_link"):
        changed_il = copy.deepcopy(il)
        changed_modules = copy.deepcopy(modules)
        if case == "missing_il": changed_il.clear()
        elif case == "invalid_signature":
            for item in changed_il: item["Status"] = "NotSigned"
        else:
            for item in changed_modules: item["fields"]["ModuleNativePath"] = ""
        try: check(changed_modules, images, changed_il)
        except AssertionError: pass
        else: raise AssertionError(case + " accepted")
    result["negative_cases"] = 3
    print(json.dumps(result, indent=2))
