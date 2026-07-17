# 07_build_master.ps1
# Roll all conference-level CSVs into one master file at Root\00_MASTER_all_companies.csv
# Deduplicates per-conference by (folder, lowercase(company)). Same company at different
# conferences stays as separate rows (they're separate leads, not duplicates).
#
# Usage (-Root is required):
#   .\07_build_master.ps1 -Root "C:\path\to\out"

param(
  [Parameter(Mandatory=$true)]
  [string]$Root   # e.g. -Root "C:\out\Conferences"  (same root used by 06_split_csv.ps1)
)

$master = New-Object System.Collections.Generic.List[object]
$seen   = New-Object System.Collections.Generic.HashSet[string]

Get-ChildItem $Root -Directory | Where-Object { $_.Name -match '^\d+_' } | ForEach-Object {
  $confDir = $_.Name
  foreach ($file in @(
    @{ Path = (Join-Path $_.FullName "exhibitors.csv");        Cat = "Exhibitor" },
    @{ Path = (Join-Path $_.FullName "sponsors_partners.csv"); Cat = $null }
  )) {
    if (-not (Test-Path $file.Path)) { continue }
    foreach ($r in (Import-Csv $file.Path)) {
      # Company name: fall back to Name column if Company is blank
      $company = if ($r.Company -and $r.Company.Trim()) { $r.Company.Trim() }
                 elseif ($r.Name -and $r.Name.Trim()) { $r.Name.Trim() }
                 else { $null }
      if (-not $company) { continue }

      $type = if ($file.Cat) { $file.Cat }
              else {
                $rawT = if ($r.Type) { $r.Type } else { "" }
                if ($rawT -like "*Partner*" -or $rawT -in @("Village","Community","Contest Organizer","Organizer") -or $rawT -like "*Media*") { "Partner" }
                else { "Sponsor" }
              }

      $key = ($confDir + "|" + $company.ToLower())
      if ($seen.Contains($key)) { continue }
      $null = $seen.Add($key)

      $master.Add([PSCustomObject]@{
        "Conference Name"     = $r."Conference Name"
        "Event Date"          = $r."Event Date"
        "City"                = $r.City
        "State"               = $r.State
        "Country"             = $r.Country
        "Type"                = $type
        "Company"             = $company
        "Company Website"     = $r."Company Website"
        "LinkedIn URL"        = $r."LinkedIn URL"
        "Company Description" = $r."Company Description"
        "Source URL"          = $r."Source URL"
        "Notes"               = $r.Notes
      })
    }
  }
}

$outPath = Join-Path $Root "00_MASTER_all_companies.csv"
$master | Export-Csv $outPath -NoTypeInformation -Encoding utf8

$exh = @($master | Where-Object { $_.Type -eq "Exhibitor" }).Count
$spo = @($master | Where-Object { $_.Type -eq "Sponsor" }).Count
$par = @($master | Where-Object { $_.Type -eq "Partner" }).Count

Write-Host ""
Write-Host "MASTER file written: $outPath" -ForegroundColor Cyan
Write-Host ("  Exhibitors:       {0,6}" -f $exh) -ForegroundColor Green
Write-Host ("  Sponsors:         {0,6}" -f $spo) -ForegroundColor Green
Write-Host ("  Partners:         {0,6}" -f $par) -ForegroundColor Green
Write-Host ("  Total unique:     {0,6}" -f $master.Count) -ForegroundColor Green
