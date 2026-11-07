param([Parameter(Mandatory=$true)][string]$RuntimeContract)
$ErrorActionPreference='Stop'
$lab='E:\codex-pair\.codex-pair\isolation'
$vmName='codex-pair-isolation-20261102'
$base='D:\Codex_No_Projects\work\github\codex-pair-real-wokey-20261104\.codex-pair\worktrees\windows-f93e3110d73b-a\validation\direct-tests-v4\hyperv-validator'
$lease=[IO.File]::Open("$lab\vm-execution.lock",'OpenOrCreate','ReadWrite','None')
$pending="$lab\vm-pending.json"
$session=$null
function Save-NewJson($path,$value){
 $bytes=[Text.Encoding]::UTF8.GetBytes(($value | ConvertTo-Json -Depth 12 -Compress))
 $stream=[IO.File]::Open($path,'CreateNew','Write','None')
 try{$stream.Write($bytes,0,$bytes.Length);$stream.Flush($true)}finally{$stream.Dispose()}
}
try {
 if(Test-Path -LiteralPath $pending){throw 'Unconfirmed prior execution; reconcile before retry'}
 $vm=Get-VM -Name $vmName
 if($vm.Id.ToString() -ne '3d337311-e8b0-4d7e-a746-f21f72e4cdb6' -or @(Get-VMNetworkAdapter -VMName $vmName).Count -ne 0 -or $vm.State -ne 'Running'){throw 'VM identity/network/state mismatch'}
 $run=[guid]::NewGuid().ToString('N')
 $output=Join-Path $base "system-images\$run"
 $snapshot=Join-Path $output 'input'
 New-Item -ItemType Directory -Path $snapshot | Out-Null
 $sources=@{'collector.ps1'="$PSScriptRoot\probe-system-image-sections.ps1";'contract.json'=$RuntimeContract;'entry.py'="$PSScriptRoot\run-system-image-entry.py";'runner.cs'="$PSScriptRoot\PairValidatorIdentityDiagnostic.cs";'reader.cs'="$lab\runner-v2\PairArtifactReader.cs";'reset.ps1'="$lab\runner-v2\Reset-Scratch.ps1"}
 foreach($name in $sources.Keys){Copy-Item -LiteralPath $sources[$name] -Destination (Join-Path $snapshot $name)}
 $hashes=@{};foreach($file in Get-ChildItem -LiteralPath $snapshot -File){$hashes[$file.Name]=(Get-FileHash -LiteralPath $file.FullName).Hash}
 $controller=(Get-FileHash -LiteralPath $PSCommandPath).Hash
 Save-NewJson "$output\request.json" @{Run=$run;VM=$vm.Id.ToString();Files=$hashes;ControllerSHA256=$controller;Formal=$false}
 $session=New-PSSession -VMName $vmName -Credential (Import-Clixml "$lab\windows-20261102\guest-credential.dpapi.xml")
 $guest="C:\PairLab\ValidatorRuns\$run"
 Invoke-Command -Session $session -ArgumentList $guest -ScriptBlock {param($path);if(Test-Path -LiteralPath $path){throw 'Run exists'};New-Item -ItemType Directory $path | Out-Null}
 foreach($file in Get-ChildItem -LiteralPath $snapshot -File){Copy-Item -ToSession $session -LiteralPath $file.FullName -Destination "$guest\$($file.Name)"}
 Save-NewJson $pending @{Run=$run;Output=$output;State='unknown';Reason='System image dispatch may have occurred; no terminal receipt yet'}
 $job=Invoke-Command -Session $session -AsJob -ArgumentList $guest,$run,$hashes -ScriptBlock {
  param($guest,$run,$hashes)
  $ErrorActionPreference='Stop'
  if($env:COMPUTERNAME -ne 'PAIR-LAB'){throw 'Guest identity mismatch'}
  foreach($name in $hashes.Keys){if((Get-FileHash -LiteralPath "$guest\$name").Hash -ne $hashes[$name]){throw 'Guest input/control digest mismatch'}}
  & icacls.exe $guest /inheritance:r /grant:r '*S-1-5-18:(OI)(CI)F' '*S-1-5-32-544:(OI)(CI)F' '*S-1-5-32-545:(OI)(CI)RX' | Out-Null
  if($LASTEXITCODE){throw 'Input ACL failed'}
  & ([scriptblock]::Create([IO.File]::ReadAllText("$guest\reset.ps1")))
  Add-Type -Path "$guest\reader.cs","$guest\runner.cs"
  $worker=Import-Clixml 'C:\PairLab\worker-credential.xml'
  $traceName='PairRestrictedSystemImageDiagnostic'
  $etl=Join-Path $env:TEMP ('pair-images-'+$run+'.etl')
  $started=$false
  try {
   & logman.exe create trace $traceName -p 'Microsoft-Windows-Kernel-Process' 0x50 5 -o $etl -ets | Out-Null
   if($LASTEXITCODE){throw 'ETW start failed'}
   $started=$true
   $native=[PairValidatorRunRunner]::Run($worker.GetNetworkCredential().Password,(Get-LocalGroup PairWriteScope).SID.Value,'validators',180,$run)
   & logman.exe stop $traceName -ets | Out-Null
   if($LASTEXITCODE){throw 'ETW stop failed'}
   $started=$false
   & tracerpt.exe $etl -o ($etl+'.xml') -of XML -summary ($etl+'.summary.xml') -y | Out-Null
   if($LASTEXITCODE){throw 'ETW conversion failed'}
   $trace=@{Xml=[IO.File]::ReadAllText($etl+'.xml');Summary=[IO.File]::ReadAllText($etl+'.summary.xml');Formal=$false}
   foreach($name in $hashes.Keys){if((Get-FileHash -LiteralPath "$guest\$name").Hash -ne $hashes[$name]){throw 'Guest input/control changed'}}
   @{Run=$run;Native=$native;Trace=$trace;Formal=$false} | ConvertTo-Json -Depth 8 -Compress
  } finally {if($started){& logman.exe stop $traceName -ets | Out-Null}}
 }
 $job | Wait-Job -Timeout 210 | Out-Null
 if($job.State -ne 'Completed'){
  if($job.State -eq 'Running'){Stop-VM -Name $vmName -TurnOff -Confirm:$false}
  throw 'System image job lacks successful terminal reply; reconcile pending run before retry'
 }
 $result=(Receive-Job $job -ErrorAction Stop) | ConvertFrom-Json
 Save-NewJson "$output\native.json" $result
 if($result.Run -ne $run -or $result.Native.ExitCode -ne 0 -or $result.Native.ActiveProcesses -ne 0 -or $result.Native.TimedOut -ne $false){throw 'System image child terminal state unverified'}
 . (Join-Path $PSScriptRoot 'Test-RootTrace.ps1')
 Test-RootTrace $result
 $manifestBytes=[Convert]::FromBase64String($result.Native.ArtifactBase64)
 Save-NewJson "$output\manifest.json" ([Text.Encoding]::UTF8.GetString($manifestBytes) | ConvertFrom-Json)
 $manifest=[Text.Encoding]::UTF8.GetString($manifestBytes) | ConvertFrom-Json
 if($manifest.formal -ne $false -or @($manifest.files.PSObject.Properties).Count -gt 64){throw 'Invalid system image manifest'}
 $bytesDirectory=Join-Path $output 'capture'
 New-Item -ItemType Directory -Path $bytesDirectory | Out-Null
 foreach($item in $manifest.files.PSObject.Properties){
  if($item.Name -notmatch '^(capture|runtime-state)\.json$|^(ntdll|kernel32|clr)\.dll\.[.A-Za-z0-9_]{1,8}\.(expected|actual)\.bin$' -or $item.Value.bytes -lt 1 -or $item.Value.bytes -gt 16777216 -or $item.Value.sha256 -notmatch '^[a-f0-9]{64}$'){throw 'Invalid system image artifact entry'}
  Invoke-Command -Session $session -ArgumentList $item.Name,$item.Value.bytes,$item.Value.sha256 -ScriptBlock {
   param($name,$length,$digest)
   $path=Join-Path 'S:\scratch\image-sections' $name
   $file=Get-Item -LiteralPath $path
   if($file.PSIsContainer -or ($file.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0 -or $file.Length -ne $length -or (Get-FileHash -LiteralPath $path).Hash -ne $digest){throw 'Guest image artifact mismatch'}
  }
  $target=Join-Path $bytesDirectory $item.Name
  Copy-Item -FromSession $session -LiteralPath ([IO.Path]::Combine('S:\scratch\image-sections',$item.Name)) -Destination $target
  if((Get-Item -LiteralPath $target).Length -ne $item.Value.bytes -or (Get-FileHash -LiteralPath $target).Hash -ne $item.Value.sha256){throw 'Copied image artifact mismatch'}
 }
 & 'D:\Anaconda3\python.exe' -I "$PSScriptRoot\check-system-image-run.py" --native "$output\native.json" --capture "$bytesDirectory\capture.json" --bytes-dir $bytesDirectory --contract "$snapshot\contract.json" --collector-sha256 $hashes['collector.ps1'].ToLowerInvariant() --output "$output\consistency.json"
 if($LASTEXITCODE){throw 'Restricted system image consistency check failed'}
 if((Get-FileHash -LiteralPath $PSCommandPath).Hash -ne $controller){throw 'Host controller changed'}
 Save-NewJson "$output\receipt.json" @{Run=$run;State='terminal';NativeSHA256=(Get-FileHash "$output\native.json").Hash;ConsistencySHA256=(Get-FileHash "$output\consistency.json").Hash;Formal=$false}
 Remove-Item -LiteralPath $pending
 @{Run=$run;Output=$output;Formal=$false} | ConvertTo-Json -Compress
} finally {
 if($session){Remove-PSSession $session}
 $lease.Dispose()
}
