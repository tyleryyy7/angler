if (-not (Get-Process -Name '数据更新' -ErrorAction SilentlyContinue)) {
    Start-Process 'D:\stockdb\数据更新.exe' -WorkingDirectory 'D:\stockdb' -WindowStyle Hidden
}
