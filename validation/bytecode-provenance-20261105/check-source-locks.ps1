$ErrorActionPreference='Stop'
$env:PSModulePath=[Environment]::GetEnvironmentVariable('PSModulePath','Machine')
$originalRoot=$PSScriptRoot
$fixture=Join-Path ([IO.Path]::GetTempPath()) ('pair-source-lock-'+[guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory $fixture | Out-Null
Copy-Item -LiteralPath $originalRoot -Destination (Join-Path $fixture 'sources') -Recurse
$fixtureRoot=Join-Path $fixture 'sources'
foreach($name in 'Invoke-ValidatorRootDiagnostic.ps1','Invoke-ValidatorCombinedDiagnostic.ps1'){
 $SourceInventory=Join-Path $fixtureRoot $(if($name -like '*Combined*'){'combined-source-inventory.json'}else{'runtime-source-inventory.json'})
 $text=[IO.File]::ReadAllText((Join-Path $PSScriptRoot $name))
 $prefix=$text.Substring($text.IndexOf('$sourceLocks='))
 $prefix=$prefix.Substring(0,$prefix.IndexOf('. (Join-Path'))
 $probe=@"
 foreach(`$path in @(`$SourceInventory)+@(`$sourceRecord.files.PSObject.Properties | ForEach-Object {Join-Path `$PSScriptRoot `$_.Name})){
  foreach(`$operation in 'write','delete'){
   `$rejected=`$false
   try {
    if(`$operation -eq 'write'){`$stream=[IO.File]::Open(`$path,'Open','Write','ReadWrite');`$stream.Dispose()}
    else{[IO.File]::Delete(`$path)}
   } catch [IO.IOException] {`$rejected=`$true}
   if(-not `$rejected){throw "Source mutation allowed"}
  }
 }
}finally{foreach(`$sourceLock in `$sourceLocks){`$sourceLock.Dispose()}}
"@
 $code=($prefix+$probe).Replace('$PSScriptRoot',("'"+$fixtureRoot.Replace("'","''")+"'"))
 Invoke-Expression $code
 foreach($path in @($SourceInventory)+@($sourceRecord.files.PSObject.Properties | ForEach-Object {Join-Path $fixtureRoot $_.Name})){
 $stream=[IO.File]::Open($path,'Open','Write','None');$stream.Dispose()
 }
 foreach($failure in 'digest','missing'){
  $record=[IO.File]::ReadAllText($SourceInventory)
  $parsed=$record | ConvertFrom-Json
  $last=@($parsed.files.PSObject.Properties)[-1]
  $missingPath=Join-Path $fixtureRoot $last.Name
  if($failure -eq 'digest'){$last.Value='0'*64;[IO.File]::WriteAllText($SourceInventory,($parsed | ConvertTo-Json -Depth 5))}
  else{Move-Item -LiteralPath $missingPath -Destination ($missingPath+'.held')}
  $failed=$false
  try {
   $controller=[IO.File]::ReadAllText((Join-Path $originalRoot $name))
   $body=$controller.Substring($controller.IndexOf('$sourceLocks='))
   $body=$body.Replace('$PSScriptRoot',("'"+$fixtureRoot.Replace("'","''")+"'"))
   Invoke-Expression $body
  }catch{$failed=$true}
  finally{
   if($failure -eq 'missing'){Move-Item -LiteralPath ($missingPath+'.held') -Destination $missingPath}
   [IO.File]::WriteAllText($SourceInventory,$record)
  }
  if(-not $failed){throw 'Invalid source accepted'}
  foreach($path in @($SourceInventory)+@($parsed.files.PSObject.Properties | ForEach-Object {Join-Path $fixtureRoot $_.Name})){
   $stream=[IO.File]::Open($path,'Open','Write','None');$stream.Dispose()
  }
 }
 Write-Output "${name}: mutation rejected; every handle released after success, digest mismatch and partial acquisition"
}

Remove-Item -LiteralPath $fixture -Recurse -Force
