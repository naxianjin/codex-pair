param([Parameter(Mandatory)][string]$InputDirectory,[Parameter(Mandatory)][string]$SourceInventory)
$ErrorActionPreference='Stop'
$sourceLocks=New-Object 'System.Collections.Generic.List[System.IDisposable]'
try {
# Keep source paths immutable through loading and terminal receipt generation.
$inventoryLock=[IO.File]::Open($SourceInventory,'Open','Read','Read')
$sourceLocks.Add($inventoryLock)
$sourceBytes=New-Object byte[] $inventoryLock.Length
if($inventoryLock.Read($sourceBytes,0,$sourceBytes.Length) -ne $sourceBytes.Length){throw 'Incomplete source inventory read'}
$sourceRecord=[Text.Encoding]::UTF8.GetString($sourceBytes) | ConvertFrom-Json
$sha=[Security.Cryptography.SHA256]::Create()
try {$sourceDigest=([BitConverter]::ToString($sha.ComputeHash($sourceBytes))).Replace('-','')} finally {$sha.Dispose()}
$requiredSources=@('Invoke-ValidatorRootDiagnostic.ps1','Test-RootTrace.ps1','PairValidatorIdentityDiagnostic.cs','root-identity-baseline.json','run-root-etw.ps1')
if(@($sourceRecord.files.PSObject.Properties).Count -ne 5 -or @($sourceRecord.files.PSObject.Properties.Name | Where-Object {$_ -notin $requiredSources}).Count){throw 'Invalid source inventory set'}
foreach($item in $sourceRecord.files.PSObject.Properties){
 $sourceLock=[IO.File]::Open((Join-Path $PSScriptRoot $item.Name),'Open','Read','Read')
 $sourceLocks.Add($sourceLock)
 $sourceHash=[Security.Cryptography.SHA256]::Create()
 try {$lockedDigest=([BitConverter]::ToString($sourceHash.ComputeHash($sourceLock))).Replace('-','')}finally{$sourceHash.Dispose()}
 if($lockedDigest -ne $item.Value){throw 'Diagnostic source inventory mismatch'}
}
. (Join-Path $PSScriptRoot 'Test-RootTrace.ps1')
$lab='E:\codex-pair\.codex-pair\isolation'
$vmName='codex-pair-isolation-20261102'
$base='D:\Codex_No_Projects\work\github\codex-pair-real-wokey-20261104\.codex-pair\worktrees\windows-f93e3110d73b-a\validation\direct-tests-v4\hyperv-validator'
$lease=[IO.File]::Open("$lab\vm-execution.lock",'OpenOrCreate','ReadWrite','None')
$session=$null
$pending="$lab\vm-pending.json"
function Save-NewJson($path,$value){
 $stream=[IO.File]::Open($path,'CreateNew','Write','None')
 try{$bytes=[Text.Encoding]::UTF8.GetBytes(($value | ConvertTo-Json -Depth 12 -Compress));$stream.Write($bytes,0,$bytes.Length);$stream.Flush($true)}finally{$stream.Dispose()}
}
try{
 if(Test-Path -LiteralPath $pending){throw 'Unconfirmed prior execution; reconcile before retry'}
 $vm=Get-VM -Name $vmName
 if($vm.Id.ToString() -ne '3d337311-e8b0-4d7e-a746-f21f72e4cdb6' -or @(Get-VMNetworkAdapter -VMName $vmName).Count -ne 0 -or $vm.State -ne 'Running'){throw 'VM identity/network/state mismatch'}
 $run=[guid]::NewGuid().ToString('N')
 $output=Join-Path $base "runs\$run"
 New-Item -ItemType Directory -Path $output | Out-Null
 $snapshot=Join-Path $output 'input';New-Item -ItemType Directory $snapshot | Out-Null
 $files=@('findings.json','coverage_ledger.json','request.json','pair_validator_worker.py','validate-findings.cjs','validate-coverage-ledger.cjs','report-schema.json','read-windows-input.cjs','read-windows-input.ps1','WindowsSafeInput.cs')
 foreach($name in $files){Copy-Item -LiteralPath (Join-Path $InputDirectory $name) -Destination (Join-Path $snapshot $name)}
 $sources=@{'runner.cs'="$PSScriptRoot\PairValidatorIdentityDiagnostic.cs";'reader.cs'="$lab\runner-v2\PairArtifactReader.cs";'reset.ps1'="$lab\runner-v2\Reset-Scratch.ps1";'entry.py'="$base\run-entry.py"}
 foreach($name in $sources.Keys){Copy-Item -LiteralPath $sources[$name] -Destination (Join-Path $snapshot $name)}
 $hashes=@{};foreach($file in Get-ChildItem -LiteralPath $snapshot -File){$hashes[$file.Name]=(Get-FileHash -LiteralPath $file.FullName).Hash}
 $identity=Get-Content -LiteralPath "$PSScriptRoot\root-identity-baseline.json" -Raw | ConvertFrom-Json
 $controlNames=@('runner.cs','entry.py','reader.cs','reset.ps1')
 if(@($identity.controls.PSObject.Properties).Count -ne 4 -or @($identity.controls.PSObject.Properties.Name | Where-Object {$_ -notin $controlNames}).Count){throw 'Invalid diagnostic control set'}
 foreach($name in $controlNames){if(-not $identity.controls.$name -or $hashes[$name] -ne $identity.controls.$name){throw 'Registered diagnostic snapshot changed'}}
 $controller=(Get-FileHash -LiteralPath $PSCommandPath).Hash
 Save-NewJson "$output\request.json" ([ordered]@{Run=$run;VM=$vm.Id.ToString();SourceInventorySHA256=$sourceDigest;ControllerSHA256=$controller;Files=$hashes;Formal=$false;Limits=@{Processes=16;ProcessMiB=384;JobMiB=512;CPUSeconds=120;WallSeconds=180;DiskMiB=64}})
 $session=New-PSSession -VMName $vmName -Credential (Import-Clixml "$lab\windows-20261102\guest-credential.dpapi.xml")
 $guest="C:\PairLab\ValidatorRuns\$run"
 Invoke-Command -Session $session -ArgumentList $guest -ScriptBlock {param($path);if(Test-Path -LiteralPath $path){throw 'Run exists'};New-Item -ItemType Directory $path | Out-Null; & icacls.exe 'C:\PairLab\ValidatorRuns' /grant '*S-1-5-32-545:(RX)' | Out-Null; if($LASTEXITCODE){throw 'Run parent traversal ACL failed'}}
 foreach($file in Get-ChildItem -LiteralPath $snapshot -File){Copy-Item -ToSession $session -LiteralPath $file.FullName -Destination "$guest\$($file.Name)"}
 Save-NewJson $pending ([ordered]@{Run=$run;Output=$output;State='unknown';Reason='Dispatch may have occurred; no terminal receipt yet'})
 $job=Invoke-Command -Session $session -AsJob -ArgumentList $guest,$run,$hashes,$identity.tools -ScriptBlock {
  param($guest,$run,$hashes,$registeredTools)
  $ErrorActionPreference='Stop'
  if($env:COMPUTERNAME -ne 'PAIR-LAB'){throw 'Guest identity mismatch'}
  foreach($name in $hashes.Keys){if((Get-FileHash -LiteralPath "$guest\$name").Hash -ne $hashes[$name]){throw 'Guest input/control digest mismatch'}}
  & icacls.exe $guest /inheritance:r /grant:r '*S-1-5-18:(OI)(CI)F' '*S-1-5-32-544:(OI)(CI)F' '*S-1-5-32-545:(OI)(CI)RX' | Out-Null
  if($LASTEXITCODE){throw 'Input ACL failed'}
  $tools=@{}
  foreach($path in 'C:\PairLab\Tools\python\python.exe','C:\PairLab\Tools\node\node.exe',"$PSHOME\powershell.exe",[object].Assembly.Location){$tools[$path]=(Get-FileHash -LiteralPath $path).Hash}
  foreach($path in $tools.Keys){if($tools[$path] -ne $registeredTools.$path){throw 'Registered diagnostic tool mismatch'}}
  & ([scriptblock]::Create([IO.File]::ReadAllText("$guest\reset.ps1")))
  Add-Type -Path "$guest\reader.cs","$guest\runner.cs"
  $quota=New-Object -ComObject Microsoft.DiskQuota;$quota.Initialize('S:',$false)
  if($quota.QuotaState -ne 2 -or $quota.DefaultQuotaLimit -ne 67108864){throw 'Quota mismatch'}
  $worker=Import-Clixml 'C:\PairLab\worker-credential.xml'
  $traceName='PairRestrictedValidatorDiagnostic'
  $etl=Join-Path $env:TEMP ('pair-validator-'+$run+'.etl')
  $traceXml=$etl+'.xml';$traceSummary=$etl+'.summary.xml';$started=$false
  try {
   & logman.exe create trace $traceName -p 'Microsoft-Windows-Kernel-Process' 0x50 5 -o $etl -ets | Out-Null
   if($LASTEXITCODE){throw 'ETW start failed'}
   $started=$true
   $native=[PairValidatorRunRunner]::Run($worker.GetNetworkCredential().Password,(Get-LocalGroup PairWriteScope).SID.Value,'validators',180,$run)
   & logman.exe stop $traceName -ets | Out-Null
   if($LASTEXITCODE){throw 'ETW stop failed'}
   $started=$false
   & tracerpt.exe $etl -o $traceXml -of XML -summary $traceSummary -y | Out-Null
   if($LASTEXITCODE){throw 'ETW conversion failed'}
   $trace=@{Xml=[IO.File]::ReadAllText($traceXml);Summary=[IO.File]::ReadAllText($traceSummary);Formal=$false}
  } finally {if($started){& logman.exe stop $traceName -ets | Out-Null}}

  foreach($name in $hashes.Keys){if((Get-FileHash -LiteralPath "$guest\$name").Hash -ne $hashes[$name]){throw 'Input/control changed'}}
  foreach($path in $tools.Keys){if((Get-FileHash -LiteralPath $path).Hash -ne $tools[$path]){throw 'Tool changed'}}
  @{Run=$run;Files=$hashes;Tools=$tools;Native=$native;Trace=$trace} | ConvertTo-Json -Depth 8 -Compress
 }
 if(-not (Wait-Job $job -Timeout 210)){
  try{Save-NewJson "$output\host-timeout.json" @{Run=$run;State='unknown';Reason='Host deadline exceeded'}}finally{Stop-VM -Name $vmName -TurnOff -Confirm:$false}
  throw 'Host deadline exceeded; reconcile pending run before retry'
 }
 $text=Receive-Job $job -ErrorAction Stop
 $result=$text | ConvertFrom-Json
 if($result.Run -ne $run -or $result.Native.ActiveProcesses -ne 0){throw 'Run/terminal mismatch'}
 Save-NewJson "$output\native.json" $result
 Test-RootTrace $result
 if($result.Native.ArtifactBase64){
  $bytes=[Convert]::FromBase64String($result.Native.ArtifactBase64)
  if($bytes.Length -gt 1048576){throw 'Artifact exceeds limit'}
  $stream=[IO.File]::Open("$output\artifact.json",'CreateNew','Write','None');try{$stream.Write($bytes,0,$bytes.Length);$stream.Flush($true)}finally{$stream.Dispose()}
 }
 if((Get-FileHash -LiteralPath $PSCommandPath).Hash -ne $controller){throw 'Host controller changed'}
 if((Get-FileHash -LiteralPath $SourceInventory).Hash -ne $sourceDigest){throw 'Source inventory changed'}
 foreach($item in $sourceRecord.files.PSObject.Properties){if((Get-FileHash -LiteralPath (Join-Path $PSScriptRoot $item.Name)).Hash -ne $item.Value){throw 'Diagnostic source changed'}}
 Save-NewJson "$output\receipt.json" @{Run=$run;State='terminal';NativeSHA256=(Get-FileHash "$output\native.json").Hash;SourceInventorySHA256=$sourceDigest;ControllerSHA256=$controller;Formal=$false}
 Remove-Item -LiteralPath $pending
 @{Run=$run;Output=$output;Formal=$false} | ConvertTo-Json -Compress
}finally{
 if($session){Remove-PSSession $session}
 $lease.Dispose()
}

} finally {
 foreach($sourceLock in $sourceLocks){$sourceLock.Dispose()}
}
