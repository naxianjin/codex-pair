$ErrorActionPreference='Stop'
. (Join-Path $PSScriptRoot 'Test-RootTrace.ps1')
$json=Get-Content (Join-Path $PSScriptRoot 'root-identity-evidence.json') -Raw
Test-RootTrace ($json | ConvertFrom-Json)
foreach($kind in 'pid','creation','lost','missing','duplicate','provider','ambiguous_loss','xml_loss') {
 $result=$json | ConvertFrom-Json
 switch($kind) {
  'pid' {$result.Native.RootPid++}
  'creation' {$result.Native.RootCreationFileTime++}
  'lost' {$result.Trace.Summary=$result.Trace.Summary -replace '(Total Events\s+Lost\s+)0','${1}1'}
  'missing' {$result.Trace.Xml='<Events />'}
  'provider' {$result.Trace.Xml=$result.Trace.Xml.Replace('{22fb2cd6-0e7b-422b-a0c7-2fad1fd0e716}','{00000000-0000-0000-0000-000000000000}')}
  'ambiguous_loss' {$result.Trace.Summary+="`nTotal Events Lost 1`n"}
  'xml_loss' {$result.Trace.Xml=$result.Trace.Xml -replace '(Name="EventsLost">)\s*0','${1}1'}
  'duplicate' {
   [xml]$doc=$result.Trace.Xml
   $ns=New-Object Xml.XmlNamespaceManager $doc.NameTable
   $ns.AddNamespace('e','http://schemas.microsoft.com/win/2004/08/events/event')
   $node=$doc.SelectSingleNode('/Events/e:Event[e:System/e:EventID="1" and normalize-space(e:EventData/e:Data[@Name="ProcessID"])="792"]',$ns)
   if(-not $node){throw 'Fixture root event missing'}
   [void]$doc.DocumentElement.AppendChild($node.CloneNode($true));$result.Trace.Xml=$doc.OuterXml
  }
 }
 $rejected=$false
 try {Test-RootTrace $result} catch {$rejected=$true}
 if(-not $rejected){throw "Invalid trace accepted: $kind"}
}
'Root trace positive and eight negative cases passed; formal=false'
