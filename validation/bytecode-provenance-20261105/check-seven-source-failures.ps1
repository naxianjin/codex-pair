$ErrorActionPreference='Stop'
$env:PSModulePath=[Environment]::GetEnvironmentVariable('PSModulePath','Machine')
$root=Join-Path ([IO.Path]::GetTempPath()) ('pair-seven-source-'+[guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory $root | Out-Null
$inventory=Get-Content -LiteralPath (Join-Path $PSScriptRoot 'raw-module-source-inventory.json') -Raw | ConvertFrom-Json
$results=@()
foreach($item in $inventory.files.PSObject.Properties){
 $changed=($inventory | ConvertTo-Json -Depth 8) | ConvertFrom-Json
 $changed.files.($item.Name)='0'*64
 $path=Join-Path $root ($item.Name+'.json')
 [IO.File]::WriteAllText($path,($changed | ConvertTo-Json -Depth 8))
 $rejected=$false
 try{& (Join-Path $PSScriptRoot 'Invoke-ValidatorRawModuleDiagnostic.ps1') -SourceInventory $path -InputDirectory $root}
 catch{if($_.Exception.Message -ne 'Diagnostic source inventory mismatch'){throw};$rejected=$true}
 if(-not $rejected){throw 'Incorrect digest accepted'}
 foreach($source in @($path)+@($inventory.files.PSObject.Properties | ForEach-Object {Join-Path $PSScriptRoot $_.Name})){
  $handle=[IO.File]::Open($source,'Open','Read','None');$handle.Dispose()
 }
 $results+=@{Source=$item.Name;RejectedBeforeDispatch=$true}
}
@{Formal=$false;Cases=$results} | ConvertTo-Json -Depth 5
foreach($case in 'missing','extra'){
 $changed=($inventory | ConvertTo-Json -Depth 8) | ConvertFrom-Json
 if($case -eq 'missing'){$changed.files.PSObject.Properties.Remove('check-restricted-raw-modules.py')}
 else{$changed.files | Add-Member -NotePropertyName 'unexpected.py' -NotePropertyValue ('0'*64)}
 $path=Join-Path $root ($case+'.json')
 [IO.File]::WriteAllText($path,($changed | ConvertTo-Json -Depth 8))
 $rejected=$false
 try{& (Join-Path $PSScriptRoot 'Invoke-ValidatorRawModuleDiagnostic.ps1') -SourceInventory $path -InputDirectory $root}
 catch{if($_.Exception.Message -ne 'Invalid source inventory set'){throw};$rejected=$true}
 if(-not $rejected){throw 'Invalid source set accepted'}
 Write-Output "$case source set rejected before dispatch"
}
