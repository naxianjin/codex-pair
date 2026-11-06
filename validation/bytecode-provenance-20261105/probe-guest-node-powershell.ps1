$ErrorActionPreference='Stop'
$env:PSModulePath=[Environment]::GetEnvironmentVariable('PSModulePath','Machine')
Import-Module Microsoft.PowerShell.Utility
$lab='E:\codex-pair\.codex-pair\isolation';$vmName='codex-pair-isolation-20261102'
if(Test-Path "$lab\vm-pending.json"){throw 'Pending execution preserved'}
$lease=[IO.File]::Open("$lab\vm-execution.lock",'OpenOrCreate','ReadWrite','None');$s=$null
try {
Start-VM -Name $vmName
Start-Sleep -Seconds 120
$s=New-PSSession -VMName $vmName -Credential (Import-Clixml "$lab\windows-20261102\guest-credential.dpapi.xml")
$result=Invoke-Command -Session $s -ScriptBlock {
$ErrorActionPreference='Stop'
function Read-Modules($process) {
 @($process.Modules | ForEach-Object { @{Path=$_.FileName;Sha256=(Get-FileHash -LiteralPath $_.FileName -Algorithm SHA256).Hash.ToLowerInvariant()} })
}
$node=$null
try {
 $node=Start-Process -FilePath 'C:\PairLab\Tools\node\node.exe' -ArgumentList '-e','setTimeout(()=>{},30000)' -PassThru -WindowStyle Hidden
 Start-Sleep -Seconds 2
 $node.Refresh()
 if($node.HasExited){throw 'Diagnostic Node exited before observation'}
 $nodeModules=Read-Modules $node
 $before=Read-Modules (Get-Process -Id $PID)
 Add-Type -TypeDefinition 'public static class PairDependencyProbe { public static int Value() { return 42; } }'
 if([PairDependencyProbe]::Value() -ne 42){throw 'Add-Type smoke test failed'}
 $after=Read-Modules (Get-Process -Id $PID)
 $assemblies=@([AppDomain]::CurrentDomain.GetAssemblies() | ForEach-Object { @{Name=$_.FullName;Location=$(if($_.IsDynamic){''}else{$_.Location})} })
 $providers=& "$env:SystemRoot\System32\logman.exe" query providers
 $providerExit=$LASTEXITCODE
 @{Formal=$false;Scope='Diagnostic snapshots, not restricted validator process tree or complete loader trace';PowerShell=$PSVersionTable.PSVersion.ToString();NodeModules=$nodeModules;PowerShellBefore=$before;PowerShellAfter=$after;Assemblies=$assemblies;AddTypeValue=42;LogmanExit=$providerExit;RelevantProviders=@($providers | Select-String -Pattern 'Kernel-Process|Kernel-Image|DotNETRuntime' | ForEach-Object {$_.ToString().Trim()})} | ConvertTo-Json -Depth 8 -Compress
} finally {if($node -and -not $node.HasExited){Stop-Process -Id $node.Id -Force}}

}
[IO.File]::WriteAllText('D:\Codex_No_Projects\work\github\codex-pair\.codex-pair\worktrees\next-recovery-20261101\validation\bytecode-provenance-20261105\guest-node-powershell-modules.json',[string]$result)
} finally {if($s){Remove-PSSession $s};Stop-VM -Name $vmName -TurnOff -Confirm:$false;$lease.Dispose()}
