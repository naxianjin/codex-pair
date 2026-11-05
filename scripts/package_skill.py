"""构建可直接解压安装的 Codex skill ZIP，不包含测试或本机协作记录。"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import tempfile
import zipfile

try:
    from .install_skill import package_manifest,checked_path,path_stat,PACKAGE_ROOTS,MANIFEST_NAME
    from .pair_audit_adapter import CLOUDFLARE_ASSETS
except ImportError:
    from install_skill import package_manifest,checked_path,path_stat,PACKAGE_ROOTS,MANIFEST_NAME
    from pair_audit_adapter import CLOUDFLARE_ASSETS


RUNTIME_ROOTS=PACKAGE_ROOTS | {MANIFEST_NAME}
HANDOFF_RUNTIME='scripts/pair_role_handoff.py'
REQUIRED={'SKILL.md','agents/openai.yaml','references/onboarding.md','references/protocol.md',
    'references/repair-multi-account.md','scripts/pair.py','scripts/pair_core.py','scripts/pair_git.py',
    'scripts/pair_transport.py','scripts/pair_windows.py','scripts/install_skill.py',
    'scripts/pair_balance.py','scripts/pair_quota.py','scripts/pair_reviewer.py',
    'scripts/pair_team.py','scripts/pair_stages.py','scripts/pair_managed.py',
    'scripts/pair_review_runtime.py','scripts/pair_workspace.py','scripts/pair_commands.py',
    'references/team-commands.md','references/validation-handoff-four-cli.md',
    'README.md','CHANGELOG.md','POST_RELEASE_TESTS.md','scripts/pair_audit_adapter.py',
    'assets/upstream-lock.json','assets/ocr-dependency.json','assets/audit/report-schema.json',
    'assets/audit/ocr-preview-schema.json','assets/audit/ocr-rules-schema.json',
    'assets/audit/validate-findings.cjs','assets/audit/validate-coverage-ledger.cjs',
    'assets/audit/read-windows-input.cjs','assets/audit/read-windows-input.ps1','assets/audit/WindowsSafeInput.cs',
    'references/audit-adapter.md','references/upstream-notices.md',
    'licenses/open-code-review-Apache-2.0.txt','licenses/cloudflare-security-audit-MIT.txt',
    'assets/upstream/ocr/WORKFLOW.md','assets/upstream/ocr/system_rules.json',
    'scripts/pair_validator_worker.py'}
REQUIRED.update('assets/upstream/cloudflare/'+name for name in CLOUDFLARE_ASSETS)


def build_package(source, output):
    source=checked_path(source).resolve(strict=True)
    output=checked_path(output)
    if output.suffix.lower()!='.zip':raise ValueError('技能包必须使用 .zip 扩展名')
    checksum=output.with_name('SHA256SUMS.txt')
    pending=output.with_name('.'+output.name+'.pending.json')
    if any(path_stat(path) is not None for path in (output,checksum,pending)):raise FileExistsError('发布包、校验或未完成标记已存在，拒绝覆盖')
    if any(c in output.name for c in '\r\n\\'):raise ValueError('校验文件名包含不支持的换行或转义字符')
    if output.is_relative_to(source):raise ValueError('输出不得位于技能源目录内')
    manifest=package_manifest(source)
    missing=(REQUIRED | {HANDOFF_RUNTIME})-manifest.keys()
    if missing:raise ValueError('技能包缺少运行文件：'+', '.join(sorted(missing)))
    output.parent.mkdir(parents=True,exist_ok=True)
    recovery=Path(tempfile.mkdtemp(prefix='.codex-pair-package-',dir=output.parent))
    archive=recovery/output.name
    sums=recovery/checksum.name
    marker=recovery/pending.name
    published=[]
    try:
        return _build_package(source,output,checksum,manifest,recovery,archive,sums,published,pending,marker)
    except Exception as error:
        # 只撤回与本次暂存文件仍为同一实体的发布项，保留完整失败现场。
        for final,staged in reversed(published):
            info=path_stat(checked_path(final))
            if info is not None and os.path.samestat(info,staged.stat()):
                final.rename(recovery/('published-'+final.name))
        if hasattr(error,'add_note'):error.add_note('打包失败现场保留：'+str(recovery))
        raise


def _build_package(source,output,checksum,manifest,recovery,archive,sums,published,pending,marker):
    with zipfile.ZipFile(archive,'x',compression=zipfile.ZIP_DEFLATED,compresslevel=9) as bundle:
        for relative in sorted(manifest):
            data=checked_path(source/relative).read_bytes()
            if hashlib.sha256(data).hexdigest()!=manifest[relative]:
                raise ValueError('构建时源文件发生变化：'+relative)
            info=zipfile.ZipInfo('codex-pair/'+relative,date_time=(2020,1,1,0,0,0))
            info.create_system=3
            info.external_attr=0o100644 << 16
            info.compress_type=zipfile.ZIP_DEFLATED
            bundle.writestr(info,data,compress_type=zipfile.ZIP_DEFLATED,compresslevel=9)
    with zipfile.ZipFile(archive) as bundle:
        if bundle.testzip() is not None:raise ValueError('ZIP 完整性校验失败')
        for relative,digest in manifest.items():
            if hashlib.sha256(bundle.read('codex-pair/'+relative)).hexdigest()!=digest:
                raise ValueError('ZIP 内文件哈希不一致：'+relative)
    digest=hashlib.sha256(archive.read_bytes()).hexdigest()
    # UTF-8无BOM、LF、hex + 两空格 + 原文件名；ASCII包名字节格式保持兼容。
    with sums.open('x',encoding='utf-8',newline='\n') as file:
        file.write(digest+'  '+output.name+'\n')
    marker.write_text(json.dumps({'status':'incomplete','recovery_directory':str(recovery)},ensure_ascii=False),encoding='utf-8')
    # 两文件不能作为一个目录项原子发布；中断时的pending标记明确禁止当成功包消费。
    for staged,final in ((marker,pending),(archive,output),(sums,checksum)):
        checked_path(final)
        # 同卷硬链接发布是原子的no-clobber；不支持时失败关闭，保留暂存包。
        os.link(staged,final)
        published.append((final,staged))
    pending.unlink()
    published.remove((pending,marker))
    result={'archive':str(output),'checksum_file':str(checksum),'sha256':digest,
        'files':manifest,'root_directory':'codex-pair'}
    try:
        archive.unlink();sums.unlink();marker.unlink();recovery.rmdir()
    except OSError:
        # 两项已完整发布；暂存清理失败不撤回成功交付，显式返回待检查路径。
        result['staging_directory']=str(recovery)
    return result


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source',default=str(Path(__file__).resolve().parent.parent))
    parser.add_argument('--output',required=True)
    args=parser.parse_args()
    print(json.dumps(build_package(args.source,args.output),ensure_ascii=False,indent=2))


if __name__=='__main__':main()
