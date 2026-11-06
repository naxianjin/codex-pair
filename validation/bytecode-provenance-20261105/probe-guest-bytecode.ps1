$ErrorActionPreference='Stop'
$env:PSModulePath=[Environment]::GetEnvironmentVariable('PSModulePath','Machine')
Import-Module Microsoft.PowerShell.Utility
$lab='E:\codex-pair\.codex-pair\isolation';$vmName='codex-pair-isolation-20261102'
if(Test-Path "$lab\vm-pending.json"){throw 'Pending execution preserved'}
$lease=[IO.File]::Open("$lab\vm-execution.lock",'OpenOrCreate','ReadWrite','None');$s=$null
try {
Start-VM -Name $vmName
Start-Sleep -Seconds 55
$s=New-PSSession -VMName $vmName -Credential (Import-Clixml "$lab\windows-20261102\guest-credential.dpapi.xml")
$result=Invoke-Command -Session $s -ScriptBlock {
$ErrorActionPreference='Stop'
$py='C:\PairLab\Tools\python\python.exe'
$code='import pathlib,struct,marshal,importlib.util,json,hashlib,sys,types
root=pathlib.Path(r''C:\PairLab\Tools\python\Lib\encodings'')
def shape(c):
 return (c.co_code,c.co_names,c.co_varnames,c.co_freevars,c.co_cellvars,c.co_flags,c.co_argcount,tuple(shape(x) if isinstance(x,types.CodeType) else x for x in c.co_consts))
results=[]
for p in sorted((root/''__pycache__'').glob(''*.pyc'')):
 data=p.read_bytes();source=root/(p.name.split(''.'')[0]+''.py'');raw=source.read_bytes();flags=struct.unpack(''<I'',data[4:8])[0];cached=marshal.loads(data[16:]);fresh=compile(raw,cached.co_filename,''exec'',dont_inherit=True,optimize=sys.flags.optimize)
 results.append({''file'':p.name,''sha256'':hashlib.sha256(data).hexdigest(),''magic_matches'':data[:4]==importlib.util.MAGIC_NUMBER,''flags'':flags,''source_size_matches'':flags==0 and struct.unpack(''<I'',data[12:16])[0]==len(raw),''source_time_matches'':flags==0 and struct.unpack(''<I'',data[8:12])[0]==(int(source.stat().st_mtime)&0xffffffff),''code_shape_matches_source'':shape(cached)==shape(fresh),''source_sha256'':hashlib.sha256(raw).hexdigest()})
import encodings.gbk
print(json.dumps({''python'':sys.version,''caches'':results,''loaded_cached_paths'':{n:getattr(sys.modules[n],''__cached__'',None) for n in (''encodings'',''encodings.aliases'',''encodings.utf_8'',''encodings.gbk'')},''formal'':False}))'
$output=& $py -I -B -c $code
if($LASTEXITCODE){throw 'Guest Python probe failed'}
@{CacheAnalysis=[string]$output;Formal=$false} | ConvertTo-Json -Depth 6 -Compress
}
[IO.File]::WriteAllText('D:\Codex_No_Projects\work\github\codex-pair\.codex-pair\worktrees\next-recovery-20261101\validation\epoch-types-20261105\guest-bytecode-analysis.json',[string]$result)
} finally {if($s){Remove-PSSession $s};Stop-VM -Name $vmName -TurnOff -Confirm:$false;$lease.Dispose()}
