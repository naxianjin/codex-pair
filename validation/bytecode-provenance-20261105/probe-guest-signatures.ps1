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
 $paths=@('C:\Windows\System32\logman.exe','C:\Windows\System32\tracerpt.exe','C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe','C:\Windows\Microsoft.NET\Framework64\v4.0.30319\mscorlib.dll','C:\PairLab\Tools\python\python.exe','C:\PairLab\Tools\node\node.exe')
 $rows=@(foreach($path in $paths){
  $signature=Get-AuthenticodeSignature -LiteralPath $path
  @{Path=$path;SHA256=(Get-FileHash -LiteralPath $path).Hash;Status=$signature.Status.ToString();StatusMessage=$signature.StatusMessage;SignerSubject=$signature.SignerCertificate.Subject;SignerThumbprint=$signature.SignerCertificate.Thumbprint;Formal=$false}
 })
 @{Files=$rows;Formal=$false;Scope='Guest Windows trust evaluation; no independent publisher baseline'} | ConvertTo-Json -Depth 6
}
[IO.File]::WriteAllText((Join-Path $PSScriptRoot 'guest-signature-observations.json'),($result -join "`n"))
Write-Output ($result -join "`n")
}finally{if($s){Remove-PSSession $s};Stop-VM -Name $vmName -TurnOff -Confirm:$false;$lease.Dispose()}
