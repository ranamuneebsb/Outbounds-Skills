# 06_split_csv.ps1
# Split every conference's exhibitors_speakers.csv into 4 category files:
#   exhibitors.csv, sponsors_partners.csv, speakers.csv, attendees.csv
#
# Usage (-Root is required):
#   .\06_split_csv.ps1 -Root "C:\path\to\out"
#
# Handles quoted commas, Type-value normalization, and Company/Name-column fallback.

param(
  [Parameter(Mandatory=$true)]
  [string]$Root   # e.g. -Root "C:\out\Conferences"  (folder holding NN_ConfName subfolders)
)

function Get-Category($rawType) {
  if ($null -eq $rawType) { return $null }
  $t = $rawType.Trim()
  if ($t -eq "Exhibitor") { return "Exhibitor" }
  if ($t -eq "Sponsor") { return "Sponsor" }
  if ($t -in @("Partner", "Community Partner", "Village", "Community", "Contest Organizer", "Organizer")) { return "Partner" }
  if ($t -in @("Sponsor/Exhibitor", "Exhibitor/Sponsor")) { return "Sponsor" }
  if ($t -eq "Sponsor (Media Partner)") { return "Partner" }
  if ($t -like "Speaker*") { return "Speaker" }
  if ($t -like "*Attendee*") { return "AttendeeStat" }
  return $null
}

function Split-Conf($dir) {
  $src = Join-Path $dir "exhibitors_speakers.csv"
  if (-not (Test-Path $src)) { return $null }
  $rows = @(Import-Csv $src)
  $header = Get-Content $src -TotalCount 1

  $exhList = New-Object System.Collections.Generic.List[object]
  $spoList = New-Object System.Collections.Generic.List[object]
  $spkList = New-Object System.Collections.Generic.List[object]
  $attList = New-Object System.Collections.Generic.List[object]

  foreach ($r in $rows) {
    $cat = Get-Category $r.Type
    switch ($cat) {
      "Exhibitor"    { $exhList.Add($r) }
      "Sponsor"      { $spoList.Add($r) }
      "Partner"      { $spoList.Add($r) }
      "Speaker"      { $spkList.Add($r) }
      "AttendeeStat" { $attList.Add($r) }
    }
  }

  # Write each file (with header preserved even when empty)
  if ($exhList.Count -gt 0) { $exhList | Export-Csv (Join-Path $dir "exhibitors.csv")        -NoTypeInformation -Encoding utf8 }
  else                      { $header  | Set-Content (Join-Path $dir "exhibitors.csv")        -Encoding utf8 }
  if ($spoList.Count -gt 0) { $spoList | Export-Csv (Join-Path $dir "sponsors_partners.csv") -NoTypeInformation -Encoding utf8 }
  else                      { $header  | Set-Content (Join-Path $dir "sponsors_partners.csv") -Encoding utf8 }
  if ($spkList.Count -gt 0) { $spkList | Export-Csv (Join-Path $dir "speakers.csv")           -NoTypeInformation -Encoding utf8 }
  else                      { $header  | Set-Content (Join-Path $dir "speakers.csv")           -Encoding utf8 }
  if ($attList.Count -gt 0) { $attList | Export-Csv (Join-Path $dir "attendees.csv")          -NoTypeInformation -Encoding utf8 }
  else                      { $header  | Set-Content (Join-Path $dir "attendees.csv")          -Encoding utf8 }

  return [PSCustomObject]@{
    Name = [System.IO.Path]::GetFileName($dir)
    Exh  = $exhList.Count
    Spo  = $spoList.Count
    Spk  = $spkList.Count
    Att  = $attList.Count
  }
}

Write-Host "Splitting CSVs under $Root..." -ForegroundColor Cyan
$results = @()
Get-ChildItem $Root -Directory | Where-Object { $_.Name -match '^\d+_' } | ForEach-Object {
  $r = Split-Conf $_.FullName
  if ($r) { $results += $r }
}
$results | Format-Table -AutoSize
Write-Host "Done. $($results.Count) conferences processed." -ForegroundColor Green
