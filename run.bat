@echo off
cd /d %~dp0
python bounty_radar.py >> radar.log 2>&1
