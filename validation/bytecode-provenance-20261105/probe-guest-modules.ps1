$ErrorActionPreference='Stop'
$env:PSModulePath=[Environment]::GetEnvironmentVariable('PSModulePath','Machine')
Import-Module Microsoft.PowerShell.Utility
$lab='E:\codex-pair\.codex-pair\isolation';$vmName='codex-pair-isolation-20261102'
if(Test-Path "$lab\vm-pending.json"){throw 'Pending execution preserved'}
$lease=[IO.File]::Open("$lab\vm-execution.lock",'OpenOrCreate','ReadWrite','None');$s=$null
try {
Start-VM -Name $vmName
Start-Sleep -Seconds 90
$s=New-PSSession -VMName $vmName -Credential (Import-Clixml "$lab\windows-20261102\guest-credential.dpapi.xml")
$result=Invoke-Command -Session $s -ScriptBlock {
$ErrorActionPreference='Stop'
$py='C:\PairLab\Tools\python\python.exe'
$code='import ctypes,ctypes.wintypes as w,json,hashlib,pathlib,sys
k=ctypes.WinDLL(''kernel32'',use_last_error=True);ps=ctypes.WinDLL(''psapi'',use_last_error=True)
k.GetCurrentProcess.restype=w.HANDLE
ps.EnumProcessModules.argtypes=[w.HANDLE,ctypes.POINTER(w.HMODULE),w.DWORD,ctypes.POINTER(w.DWORD)]
ps.GetModuleFileNameExW.argtypes=[w.HANDLE,w.HMODULE,w.LPWSTR,w.DWORD]
modules=(w.HMODULE*2048)();needed=w.DWORD();h=k.GetCurrentProcess()
if not ps.EnumProcessModules(h,modules,ctypes.sizeof(modules),ctypes.byref(needed)):raise ctypes.WinError(ctypes.get_last_error())
if needed.value>ctypes.sizeof(modules):raise RuntimeError(''Module inventory exceeds fixed capacity'')
result=[]
for module in modules[:needed.value//ctypes.sizeof(w.HMODULE)]:
 buf=ctypes.create_unicode_buffer(32768)
 if not ps.GetModuleFileNameExW(h,module,buf,len(buf)):raise ctypes.WinError(ctypes.get_last_error())
 p=pathlib.Path(buf.value);result.append({''path'':str(p),''sha256'':hashlib.sha256(p.read_bytes()).hexdigest()})
print(json.dumps({''python'':sys.version,''modules'':result,''scope'':''diagnostic Python process at one instant; not restricted validator tree'',''formal'':False}))'
$output=& $py -I -B -c $code
if($LASTEXITCODE){throw 'Guest Python probe failed'}
@{CacheAnalysis=[string]$output;Formal=$false} | ConvertTo-Json -Depth 6 -Compress
}
[IO.File]::WriteAllText('D:\Codex_No_Projects\work\github\codex-pair\.codex-pair\worktrees\next-recovery-20261101\validation\epoch-types-20261105\guest-python-modules.json',[string]$result)
} finally {if($s){Remove-PSSession $s};Stop-VM -Name $vmName -TurnOff -Confirm:$false;$lease.Dispose()}
