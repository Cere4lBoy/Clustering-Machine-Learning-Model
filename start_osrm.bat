@echo off
rem Starts our own OSRM road server on http://127.0.0.1:5000 (only reachable from this laptop).
rem The notebooks and the dashboard ask it for road distances.
rem Keep this window open while a model runs. Close it to stop the server.
rem
rem One-time set-up (everything goes in D:\osrm):
rem   1. "%USERPROFILE%\anaconda3\python.exe" -m venv D:\osrm\venv
rem   2. D:\osrm\venv\Scripts\python.exe -m pip install --pre --only-binary :all: osrm-bindings
rem   3. Download malaysia-singapore-brunei-latest.osm.pbf from https://download.geofabrik.de/asia/ into D:\osrm
rem   4. Copy the folder venv\Lib\site-packages\osrm\share\profiles to D:\osrm\profiles. In the copy of car.lua,
rem      add the line "track = 10," to the highway speeds, below "service = 15,". Tracks (estate and plantation
rem      roads) are then drivable at 10 km/h, the same as on the public OSRM demo server.
rem   5. In D:\osrm, prepare the map (about 7 minutes):
rem        venv\Scripts\python.exe -m osrm extract malaysia-singapore-brunei-latest.osm.pbf -p profiles\car.lua
rem        venv\Scripts\python.exe -m osrm partition malaysia-singapore-brunei-latest.osrm
rem        venv\Scripts\python.exe -m osrm customize malaysia-singapore-brunei-latest.osrm
rem   To pick up new roads: download the map again and repeat step 5, then delete
rem   outputs\cache\osrm_cache.json so the distances are fetched again.

set OSRM_DIR=D:\osrm
set OSRM_MAP=%OSRM_DIR%\malaysia-singapore-brunei-latest.osrm

if not exist "%OSRM_DIR%\venv\Scripts\python.exe" (
    echo OSRM is not installed in %OSRM_DIR%. See the set-up steps at the top of this file.
    pause
    exit /b 1
)

echo Starting the road server on http://127.0.0.1:5000 ...
"%OSRM_DIR%\venv\Scripts\python.exe" -m osrm routed --algorithm mld --max-table-size 1000 --ip 127.0.0.1 --port 5000 "%OSRM_MAP%"
pause
