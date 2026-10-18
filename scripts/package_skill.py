"""构建可直接解压安装的 Codex skill ZIP，不包含测试或本机协作记录。"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import zipfile

try:
    from .install_skill import package_manifest
except ImportError:
    from install_skill import package_manifest


RUNTIME_ROOTS={'SKILL.md','agents','scripts','references'}
REQUIRED={'SKILL.md','agents/openai.yaml','references/onboarding.md','references/protocol.md',
    'references/repair-multi-account.md','scripts/pair.py','scripts/pair_core.py','scripts/pair_git.py',
    'scripts/pair_transport.py','scripts/pair_windows.py','scripts/install_skill.py',
    'scripts/pair_balance.py','scripts/pair_quota.py','scripts/pair_members.py','references/members.md'}


def build_package(source, output):
    source=Path(source).resolve(strict=True)
    output=Path(output).absolute()
    if output.suffix.lower()!='.zip':raise ValueError('技能包必须使用 .zip 扩展名')
    if output.resolve()!=output:raise ValueError('输出路径包含重定向')
    checksum=output.with_name('SHA256SUMS.txt')
    if output.exists() or checksum.exists():raise FileExistsError('发布包或校验文件已存在，拒绝覆盖')
    manifest={name:digest for name,digest in package_manifest(source).items()
        if Path(name).parts[0] in RUNTIME_ROOTS}
    missing=REQUIRED-manifest.keys()
    if missing:raise ValueError('技能包缺少运行文件：'+', '.join(sorted(missing)))
    output.parent.mkdir(parents=True,exist_ok=True)
    with zipfile.ZipFile(output,'x',compression=zipfile.ZIP_DEFLATED,compresslevel=9) as bundle:
        for relative in sorted(manifest):
            data=(source/relative).read_bytes()
            if hashlib.sha256(data).hexdigest()!=manifest[relative]:
                raise ValueError('构建时源文件发生变化：'+relative)
            info=zipfile.ZipInfo('codex-pair/'+relative,date_time=(2020,1,1,0,0,0))
            info.create_system=3
            info.external_attr=0o100644 << 16
            info.compress_type=zipfile.ZIP_DEFLATED
            bundle.writestr(info,data,compress_type=zipfile.ZIP_DEFLATED,compresslevel=9)
    with zipfile.ZipFile(output) as bundle:
        if bundle.testzip() is not None:raise ValueError('ZIP 完整性校验失败')
        for relative,digest in manifest.items():
            if hashlib.sha256(bundle.read('codex-pair/'+relative)).hexdigest()!=digest:
                raise ValueError('ZIP 内文件哈希不一致：'+relative)
    digest=hashlib.sha256(output.read_bytes()).hexdigest()
    with checksum.open('x',encoding='ascii',newline='\n') as file:
        file.write(digest+'  '+output.name+'\n')
    return {'archive':str(output),'checksum_file':str(checksum),'sha256':digest,
        'files':manifest,'root_directory':'codex-pair'}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source',default=str(Path(__file__).resolve().parent.parent))
    parser.add_argument('--output',required=True)
    args=parser.parse_args()
    print(json.dumps(build_package(args.source,args.output),ensure_ascii=False,indent=2))


if __name__=='__main__':main()
