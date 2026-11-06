$ErrorActionPreference='Stop'
$env:PSModulePath=[Environment]::GetEnvironmentVariable('PSModulePath','Machine')
Import-Module Microsoft.PowerShell.Utility
$lab='E:\codex-pair\.codex-pair\isolation';$vmName='codex-pair-isolation-20261102'
if(Test-Path "$lab\vm-pending.json"){throw 'Pending execution preserved'}
$lease=[IO.File]::Open("$lab\vm-execution.lock",'OpenOrCreate','ReadWrite','None');$s=$null
try {
Start-VM -Name $vmName
Start-Sleep -Seconds 160
$s=New-PSSession -VMName $vmName -Credential (Import-Clixml "$lab\windows-20261102\guest-credential.dpapi.xml")
$result=Invoke-Command -Session $s -ScriptBlock {
 $ErrorActionPreference='Stop'
 $paths=@('C:\Windows\Microsoft.Net\assembly\GAC_64\System.Data\v4.0_4.0.0.0__b77a5c561934e089\System.Data.dll','C:\Windows\Microsoft.Net\assembly\GAC_64\System.Transactions\v4.0_4.0.0.0__b77a5c561934e089\System.Transactions.dll','C:\Windows\Microsoft.Net\assembly\GAC_64\mscorlib\v4.0_4.0.0.0__b77a5c561934e089\mscorlib.dll','C:\Windows\Microsoft.Net\assembly\GAC_MSIL\Microsoft.Management.Infrastructure\v4.0_1.0.0.0__31bf3856ad364e35\Microsoft.Management.Infrastructure.dll','C:\Windows\Microsoft.Net\assembly\GAC_MSIL\Microsoft.PowerShell.Commands.Management\v4.0_3.0.0.0__31bf3856ad364e35\Microsoft.PowerShell.Commands.Management.dll','C:\Windows\Microsoft.Net\assembly\GAC_MSIL\Microsoft.PowerShell.Commands.Utility\v4.0_3.0.0.0__31bf3856ad364e35\Microsoft.PowerShell.Commands.Utility.dll','C:\Windows\Microsoft.Net\assembly\GAC_MSIL\Microsoft.PowerShell.ConsoleHost.resources\v4.0_3.0.0.0_zh-Hans_31bf3856ad364e35\Microsoft.PowerShell.ConsoleHost.resources.dll','C:\Windows\Microsoft.Net\assembly\GAC_MSIL\Microsoft.PowerShell.ConsoleHost\v4.0_3.0.0.0__31bf3856ad364e35\Microsoft.PowerShell.ConsoleHost.dll','C:\Windows\Microsoft.Net\assembly\GAC_MSIL\Microsoft.PowerShell.Security\v4.0_3.0.0.0__31bf3856ad364e35\Microsoft.PowerShell.Security.dll','C:\Windows\Microsoft.Net\assembly\GAC_MSIL\System.Configuration.Install\v4.0_4.0.0.0__b03f5f7f11d50a3a\System.Configuration.Install.dll','C:\Windows\Microsoft.Net\assembly\GAC_MSIL\System.Configuration\v4.0_4.0.0.0__b03f5f7f11d50a3a\System.Configuration.dll','C:\Windows\Microsoft.Net\assembly\GAC_MSIL\System.Core\v4.0_4.0.0.0__b77a5c561934e089\System.Core.dll','C:\Windows\Microsoft.Net\assembly\GAC_MSIL\System.DirectoryServices\v4.0_4.0.0.0__b03f5f7f11d50a3a\System.DirectoryServices.dll','C:\Windows\Microsoft.Net\assembly\GAC_MSIL\System.Management.Automation.resources\v4.0_3.0.0.0_zh-Hans_31bf3856ad364e35\System.Management.Automation.resources.dll','C:\Windows\Microsoft.Net\assembly\GAC_MSIL\System.Management.Automation\v4.0_3.0.0.0__31bf3856ad364e35\System.Management.Automation.dll','C:\Windows\Microsoft.Net\assembly\GAC_MSIL\System.Management\v4.0_4.0.0.0__b03f5f7f11d50a3a\System.Management.dll','C:\Windows\Microsoft.Net\assembly\GAC_MSIL\System.Numerics\v4.0_4.0.0.0__b77a5c561934e089\System.Numerics.dll','C:\Windows\Microsoft.Net\assembly\GAC_MSIL\System.Xml\v4.0_4.0.0.0__b77a5c561934e089\System.Xml.dll','C:\Windows\Microsoft.Net\assembly\GAC_MSIL\System.resources\v4.0_4.0.0.0_zh-Hans_b77a5c561934e089\System.resources.dll','C:\Windows\Microsoft.Net\assembly\GAC_MSIL\System\v4.0_4.0.0.0__b77a5c561934e089\System.dll','C:\Windows\Microsoft.Net\assembly\GAC_MSIL\mscorlib.resources\v4.0_4.0.0.0_zh-Hans_b77a5c561934e089\mscorlib.resources.dll')
 $rows=@(foreach($path in $paths){
  $signature=Get-AuthenticodeSignature -LiteralPath $path
  @{Path=$path;SHA256=(Get-FileHash -LiteralPath $path).Hash;Status=$signature.Status.ToString();StatusMessage=$signature.StatusMessage;SignerSubject=$signature.SignerCertificate.Subject;SignerThumbprint=$signature.SignerCertificate.Thumbprint;Formal=$false}
 })
 @{Files=$rows;Formal=$false;Scope='Guest Windows trust evaluation; no independent publisher baseline'} | ConvertTo-Json -Depth 6
}
[IO.File]::WriteAllText((Join-Path $PSScriptRoot 'guest-il-content-observations.json'),($result -join "`n"))
Write-Output ($result -join "`n")
}finally{if($s){Remove-PSSession $s};Stop-VM -Name $vmName -TurnOff -Confirm:$false;$lease.Dispose()}
