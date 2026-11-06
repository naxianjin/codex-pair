param()
$ErrorActionPreference='Stop'
$source=Join-Path $PSScriptRoot '../../assets/audit/WindowsSafeInput.cs'
$stream=[IO.File]::Open($source,'Open','Read','Read')
try {
 $memory=New-Object IO.MemoryStream
 $stream.CopyTo($memory)
 $bytes=$memory.ToArray()
 $sha=[Security.Cryptography.SHA256]::Create()
 $sourceHash=[BitConverter]::ToString($sha.ComputeHash($bytes)).Replace('-','').ToLowerInvariant()
 Add-Type -TypeDefinition ([Text.Encoding]::UTF8.GetString($bytes))
 $type=[PairAuditInput]
 $methods=@($type.GetMethods([Reflection.BindingFlags]'Static,Public,NonPublic,DeclaredOnly') | ForEach-Object {
  $body=$_.GetMethodBody()
  if($body){$il=$body.GetILAsByteArray();@{Name=$_.ToString();Token=$_.MetadataToken;ILBytes=$il.Length;ILSHA256=[BitConverter]::ToString($sha.ComputeHash($il)).Replace('-','').ToLowerInvariant()}}
 })
 if($methods.Count -ne 3){throw 'Unexpected managed method count'}
 $fixture=Join-Path ([IO.Path]::GetTempPath()) ('pair-dynamic-'+[guid]::NewGuid().ToString('N')+'.json')
 [IO.File]::WriteAllBytes($fixture,[Text.Encoding]::UTF8.GetBytes('[]'))
 $read=[PairAuditInput]::Read($fixture)
 if([Text.Encoding]::UTF8.GetString($read) -ne '[]'){throw 'Compiled reader output mismatch'}
 $writeRejected=$false
 try {$writer=[IO.File]::Open($source,'Open','Write','ReadWrite');$writer.Dispose()}catch [IO.IOException]{$writeRejected=$true}
 if(-not $writeRejected){throw 'Source lock did not reject writer'}
 @{Formal=$false;Scope='Current local diagnostic compilation only';SourceSHA256=$sourceHash;Assembly=$type.Assembly.FullName;MVID=$type.Module.ModuleVersionId.ToString();Methods=$methods;ReadPassed=$true;SourceWriterRejected=$writeRejected;Limit='Reflection IL does not prove JIT native bytes, historical dynamic modules or official host identity.'} | ConvertTo-Json -Depth 6
} finally {$stream.Dispose()}
