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
$traceName='PairDependencyDiagnostic'
$etl=Join-Path $env:TEMP 'pair-dependency-diagnostic.etl'
$xml=Join-Path $env:TEMP 'pair-dependency-diagnostic.xml'
$started=$false
try {
 $providerFile=Join-Path $env:TEMP 'pair-loader-providers.txt'
 [IO.File]::WriteAllLines($providerFile,@('"Microsoft-Windows-Kernel-Process" 0x50 5','"Microsoft-Windows-DotNETRuntime" 0x8 5'))
 & logman.exe create trace $traceName -pf $providerFile -o $etl -ets | Out-Null
 if($LASTEXITCODE){throw 'ETW start failed'}
 $started=$true
 $js=Join-Path $env:TEMP 'pair-etw-tree.js'
 [IO.File]::WriteAllText($js,'const cp=require("child_process");const r=cp.spawnSync(process.env.SystemRoot+"\\System32\\WindowsPowerShell\\v1.0\\powershell.exe",["-NoProfile","-NonInteractive","-Command","Add-Type -TypeDefinition ''public static class PairEtwTree { public static int Value() { return 42; } }''; if ([PairEtwTree]::Value() -ne 42) { exit 1 }"],{windowsHide:true});process.exit(r.status===0?0:1);')
 $child=Start-Process 'C:\PairLab\Tools\node\node.exe' -ArgumentList $js -PassThru -Wait -WindowStyle Hidden
 & logman.exe stop $traceName -ets | Out-Null
 if($LASTEXITCODE){throw 'ETW stop failed'}
 $started=$false
 $summary=Join-Path $env:TEMP 'pair-etw-tree-summary.xml'
 & tracerpt.exe $etl -o $xml -of XML -summary $summary -y | Out-Null
 if($LASTEXITCODE){throw 'ETW conversion failed'}
 [xml]$events=Get-Content -LiteralPath $xml -Raw
 $all=@($events.SelectNodes('//*[local-name()="Event"]'))
 $matched=$all
 $paths=@($all | Where-Object {$_.System.Provider.Name -eq 'Microsoft-Windows-Kernel-Process' -and $_.System.EventID -eq '5'} | ForEach-Object {$_.EventData.Data | Where-Object {$_.Name -eq 'ImageName'} | ForEach-Object {$_.InnerText}} | Sort-Object -Unique)
 $images=@(foreach($path in $paths){
  $diskPath=$path -replace '^\\Device\\HarddiskVolume4','C:'
  if(Test-Path -LiteralPath $diskPath -PathType Leaf){@{Image=$path;DiskPath=$diskPath;SHA256=(Get-FileHash -LiteralPath $diskPath).Hash}}
  else {@{Image=$path;DiskPath=$diskPath;Missing=$true}}
 })
 $traceTools=@{};foreach($tool in 'logman.exe','tracerpt.exe'){$path=Join-Path $env:SystemRoot ('System32\'+$tool);$traceTools[$path]=(Get-FileHash -LiteralPath $path).Hash}

 @{Images=$images;TraceTools=$traceTools;Formal=$false;ChildPid=$child.Id;ChildExit=$child.ExitCode;EventCount=$all.Count;PidTextMatchedEvents=@($matched | ForEach-Object {$_.OuterXml});Summary=[IO.File]::ReadAllText($summary);Scope='Diagnostic elevated Node to PowerShell compilation tree; not restricted validator'} | ConvertTo-Json -Depth 8 -Compress
} finally {if($started){& logman.exe stop $traceName -ets | Out-Null}}

}
[IO.File]::WriteAllText('D:\Codex_No_Projects\work\github\codex-pair\.codex-pair\worktrees\next-recovery-20261101\validation\bytecode-provenance-20261105\guest-combined-loader.json',[string]$result)
} finally {if($s){Remove-PSSession $s};Stop-VM -Name $vmName -TurnOff -Confirm:$false;$lease.Dispose()}
