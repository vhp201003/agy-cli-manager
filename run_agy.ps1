<#
.SYNOPSIS
    run_agy.ps1: Run agy via agy-cli-manager proxy if available, else fall back to plain agy.
#>
param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$AgyArgs
)

[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$bundlePath = "D:\CenNextWorkSpace\agy-cli-manager\certs\bundle.crt"

$isProxyRunning = $false
try {
    $tcp = New-Object System.Net.Sockets.TcpClient
    $tcp.Connect("127.0.0.1", 8899)
    $isProxyRunning = $true
    $tcp.Close()
} catch {
    $isProxyRunning = $false
}

if ($isProxyRunning) {
    $env:HTTPS_PROXY   = "http://127.0.0.1:8899"
    $env:HTTP_PROXY    = "http://127.0.0.1:8899"
    $env:SSL_CERT_FILE = $bundlePath
    Write-Host "[AGY] Proxy active -> http://127.0.0.1:8899 | Dashboard: http://127.0.0.1:8800" -ForegroundColor Cyan
} else {
    $env:HTTPS_PROXY   = ""
    $env:HTTP_PROXY    = ""
    $env:SSL_CERT_FILE = ""
    Write-Host "[AGY] No proxy detected, running standard agy." -ForegroundColor DarkGray
}

if ($AgyArgs.Count -gt 0) {
    & agy.exe @AgyArgs
} else {
    & agy.exe
}
