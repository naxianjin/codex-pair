$ErrorActionPreference='Stop'
$controller=[IO.File]::ReadAllText((Join-Path $PSScriptRoot 'Invoke-ValidatorRawModuleDiagnostic.ps1'))
$lines=$controller -split "`n"
$gate=($lines | Where-Object {$_ -like '*python.exe*check-restricted-raw-modules.py*' -or $_ -like '*Raw module evidence verification failed*'}) -join "`n"
if(-not $gate){throw 'Actual gate not found'}
$fixture=Join-Path ([IO.Path]::GetTempPath()) ('pair-raw-gate-'+[guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory $fixture | Out-Null
$original=[IO.File]::ReadAllText((Join-Path $PSScriptRoot 'restricted-raw-module-evidence.json'))
foreach($case in 'positive','missing_events','unknown_version'){
 $data=$original | ConvertFrom-Json
 if($case -eq 'missing_events'){$data.Trace.ModuleEvents=@()}
 if($case -eq 'unknown_version'){foreach($event in $data.Trace.ModuleEvents){$event.Version=999}}
 [IO.File]::WriteAllText((Join-Path $fixture 'native.json'),($data | ConvertTo-Json -Depth 20 -Compress))
 $output=$fixture
 $code=$gate.Replace('$PSScriptRoot',("'"+$PSScriptRoot.Replace("'","''")+"'"))
 $receipt=Join-Path $fixture ($case+'-receipt.json')
 $pending=Join-Path $fixture ($case+'-pending.json')
 [IO.File]::WriteAllText($pending,'pending')
 $accepted=$false
 try {Invoke-Expression $code;[IO.File]::WriteAllText($receipt,'terminal');[IO.File]::Delete($pending);$accepted=$true}catch {if($case -eq 'positive'){throw}}
 if($accepted -ne ($case -eq 'positive')){throw "Unexpected gate result: $case"}
 if((Test-Path -LiteralPath $receipt) -ne $accepted){throw 'Unexpected receipt state'}
 if((Test-Path -LiteralPath $pending) -eq $accepted){throw 'Unexpected pending state'}
 Write-Output "$case accepted=$accepted; fixture receipt/pending correct; formal=false"
}
