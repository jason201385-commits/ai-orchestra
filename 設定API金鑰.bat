@echo off
chcp 65001 >nul
cd /d "%~dp0"

REM 雙擊即可。key 走遮蔽輸入,不進命令列、不進歷史檔、不寫任何檔案。
REM 細節與安全邊界見 scripts\set_api_key.py 的說明。

where python >nul 2>&1
if errorlevel 1 (
  echo 找不到 python。請先安裝 Python 3.11+ 並確認它在 PATH 上。
  echo.
  pause
  exit /b 1
)

REM 有帶參數就直接轉給腳本
if not "%~1"=="" (
  python "scripts\set_api_key.py" %*
  echo.
  pause
  exit /b
)

REM 沒帶參數:先列出哪幾家要 key,再問要設哪一家。
REM 這裡刻意不用括號區塊 —— 區塊內的 %VAR% 會在解析期就展開,讀不到剛 set 的值。
python "scripts\set_api_key.py"
echo.
set "PROVIDER="
set /p "PROVIDER=要設定哪一家(直接 Enter 取消): "
if not defined PROVIDER goto :end
python "scripts\set_api_key.py" "%PROVIDER%"

:end
echo.
pause
