param()
$ErrorActionPreference='Stop'
$source=Join-Path $PSScriptRoot '../../assets/audit/WindowsSafeInput.cs'
$workspace=Join-Path ([IO.Path]::GetTempPath()) ('pair-loaded-bytes-'+[guid]::NewGuid().ToString('N'))
[IO.Directory]::CreateDirectory($workspace) | Out-Null
$assemblyPath=Join-Path $workspace 'reader.dll'
$sha=[Security.Cryptography.SHA256]::Create()
function Hash-Bytes([byte[]]$data){[BitConverter]::ToString($sha.ComputeHash($data)).Replace('-','').ToLowerInvariant()}
$sourceStream=[IO.File]::Open($source,'Open','Read','Read')
try {
 $memory=New-Object IO.MemoryStream
 try {$sourceStream.CopyTo($memory);$sourceBytes=$memory.ToArray()} finally {$memory.Dispose()}
 Add-Type -TypeDefinition ([Text.Encoding]::UTF8.GetString($sourceBytes)) -OutputAssembly $assemblyPath
 $dll=[IO.File]::Open($assemblyPath,'Open','Read','Read')
 try {
  $memory=New-Object IO.MemoryStream
  try {$dll.CopyTo($memory);$assemblyBytes=$memory.ToArray()} finally {$memory.Dispose()}
  $loaded=[Reflection.Assembly]::Load($assemblyBytes)
  $type=$loaded.GetType('PairAuditInput',$true)
  $methods=@($type.GetMethods([Reflection.BindingFlags]'Static,Public,NonPublic,DeclaredOnly') | ForEach-Object {
   $body=$_.GetMethodBody()
   if($body){$il=$body.GetILAsByteArray();@{Token=$_.MetadataToken;Name=$_.ToString();ILSHA256=(Hash-Bytes $il);ILBytes=$il.Length}}
  })
  if($methods.Count -ne 3){throw 'Managed reader method set mismatch'}
  $fixturePath=Join-Path $workspace 'input.json';[IO.File]::WriteAllText($fixturePath,'[]',(New-Object Text.UTF8Encoding($false)))
  $arguments=New-Object object[] 1
  $arguments[0]=[string]$fixturePath
  $read=$type.GetMethod('Read').Invoke($null,$arguments)
  if([Text.Encoding]::UTF8.GetString($read) -ne '[]'){throw 'Loaded byte reader failed'}
  $invalid=New-Object byte[] $assemblyBytes.Length
  $rejected=$false
  try {[Reflection.Assembly]::Load($invalid) | Out-Null}catch [BadImageFormatException] {$rejected=$true}catch [Reflection.TargetInvocationException] {if($_.Exception.InnerException -isnot [BadImageFormatException]){throw};$rejected=$true}
  if(-not $rejected){throw 'Invalid assembly bytes accepted'}
  $images=@(Get-Process -Id $PID -Module | Where-Object {$_.ModuleName -in @('ntdll.dll','kernel32.dll','clr.dll')} | ForEach-Object {
   $module=$_
   $disk=[IO.File]::Open($module.FileName,'Open','Read','Read')
   try {
    $diskHash=[BitConverter]::ToString($sha.ComputeHash($disk)).Replace('-','').ToLowerInvariant()
    $mapped=New-Object byte[] $module.ModuleMemorySize
    [Runtime.InteropServices.Marshal]::Copy($module.BaseAddress,$mapped,0,$mapped.Length)
    @{Name=$module.ModuleName;Path=$module.FileName;BaseAddress=$module.BaseAddress.ToInt64().ToString();MappedBytes=$mapped.Length;MappedSHA256=(Hash-Bytes $mapped);DiskSHA256=$diskHash}
   } finally {$disk.Dispose()}
  })
  if($images.Count -ne 3){throw 'Expected three system modules'}
  @{Formal=$false;PID=$PID;ProcessCreationUTC=(Get-Process -Id $PID).StartTime.ToUniversalTime().ToString('o');SourceSHA256=(Hash-Bytes $sourceBytes);AssemblyInputSHA256=(Hash-Bytes $assemblyBytes);MVID=$type.Module.ModuleVersionId.ToString();Methods=$methods;Images=$images;ReadPassed=$true;InvalidAssemblyRejected=$rejected;Limit='Current collector process only. Memory hashes are observations, not PE equivalence, trusted baseline, JIT authentication or official host receipts.'} | ConvertTo-Json -Depth 7
 } finally {$dll.Dispose()}
} finally {$sourceStream.Dispose();$sha.Dispose()}
