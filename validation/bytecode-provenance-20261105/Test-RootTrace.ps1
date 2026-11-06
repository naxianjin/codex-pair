function Test-RootTrace($result) {
 $ns=New-Object Xml.XmlNamespaceManager ([xml]$result.Trace.Xml).NameTable
 $ns.AddNamespace('e','http://schemas.microsoft.com/win/2004/08/events/event')
 [xml]$document=$result.Trace.Xml
 $counts=[regex]::Matches($result.Trace.Summary,'(?m)^Total Events[^\S\r\n]+Lost[^\S\r\n]+(\d+)[ \t\r]*$')
 if($counts.Count -ne 1 -or $counts[0].Groups[1].Value -ne '0'){throw 'ETW loss count missing, ambiguous or nonzero'}
 foreach($loss in $document.SelectNodes('//e:Data[@Name="EventsLost"]',$ns)){if($loss.InnerText.Trim() -ne '0'){throw 'XML ETW loss count nonzero'}}
 $matches=@($document.SelectNodes('/Events/e:Event[e:System/e:EventID="1" and e:System/e:Provider[@Guid="{22fb2cd6-0e7b-422b-a0c7-2fad1fd0e716}"]]',$ns) | Where-Object {
  $fields=@{};foreach($item in $_.SelectNodes('e:EventData/e:Data',$ns)){$fields[$item.Name]=$item.InnerText.Trim()}
  $fields['ProcessID'] -eq [string]$result.Native.RootPid -and
   [DateTime]::Parse($fields['CreateTime'],[Globalization.CultureInfo]::InvariantCulture,[Globalization.DateTimeStyles]::AdjustToUniversal).ToFileTimeUtc() -eq $result.Native.RootCreationFileTime
 })
 if($matches.Count -ne 1){throw 'Native root identity does not match exactly one ETW start'}
}
