@echo off
chcp 65001 >nul
title 围棋 AI 智能教练
cd /d "%~dp0"

echo ============================================
echo   围棋 AI 智能教练 启动中...
echo ============================================

REM 检查 Flask 是否已安装
python -c "import flask" 2>nul
if errorlevel 1 (
    echo [提示] 未检测到 Flask，正在安装依赖...
    pip install -r requirements.txt
)

python app.py

pause
