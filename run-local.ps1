param(
    [int]$Port = 5000
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"

if (-not (Test-Path $Python)) {
    $Python = "python"
}

$env:DATABASE_BACKEND = "sqlite"
$env:STORAGE_BACKEND = "local"
$env:SQLITE_PATH = Join-Path $ProjectRoot "data\memory_house.db"

Write-Host "Local database: $env:SQLITE_PATH"
Write-Host "Local uploads:  $(Join-Path $ProjectRoot 'static\uploads')"
Write-Host "This computer: http://127.0.0.1:$Port/"

$ipconfig = ipconfig
$lanIps = $ipconfig | Select-String -Pattern "IPv4 Address.*: ([0-9]+\.[0-9]+\.[0-9]+\.[0-9]+)" | ForEach-Object {
    $_.Matches[0].Groups[1].Value
} | Where-Object {
    $_ -ne "127.0.0.1"
} | Select-Object -Unique

foreach ($ip in $lanIps) {
    Write-Host "Other device:  http://$ip`:$Port/"
}

& $Python -m flask --app app run --host 0.0.0.0 --port $Port
