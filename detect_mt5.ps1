$ErrorActionPreference = "SilentlyContinue"
# Detect an already running MT5 terminal first. Do not require the EXE path
# to contain "Exness"; the Python engine validates the actual broker server.
$running = Get-CimInstance Win32_Process -Filter "Name='terminal64.exe'" |
  ForEach-Object { $_.ExecutablePath } |
  Where-Object { $_ -and (Test-Path -LiteralPath $_) } |
  Select-Object -First 1
if ($running) { Write-Output $running; exit 0 }

# Otherwise search common Windows install locations.
$roots = @(
  [Environment]::GetEnvironmentVariable('ProgramFiles'),
  [Environment]::GetEnvironmentVariable('ProgramFiles(x86)'),
  [Environment]::GetEnvironmentVariable('APPDATA'),
  [Environment]::GetEnvironmentVariable('LOCALAPPDATA')
) | Where-Object { $_ -and (Test-Path -LiteralPath $_) } | Select-Object -Unique

foreach ($root in $roots) {
  $hit = Get-ChildItem -LiteralPath $root -Filter terminal64.exe -File -Recurse -ErrorAction SilentlyContinue |
         Select-Object -First 1 -ExpandProperty FullName
  if ($hit) { Write-Output $hit; exit 0 }
}
exit 1
