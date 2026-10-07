@echo off
rem Ярлык для запуска окна управления базой двойным щелчком.
rem Лежит в корне базы, программа - в tools\dbapp.
rem
rem Python программа ищет сама. Имя пользователя и папка установки
rem нигде не прописаны, поэтому файл работает на любом компьютере.
rem Если подходящий Python не найден - будет понятное сообщение.
rem
rem Внимание: имена файлов с русскими буквами cmd находит не всегда -
rem мешает кодировка консоли. Поэтому запускаем по имени main.py
rem (только латиница), а рабочую папку задаём заранее.
rem
rem Внимание: вертикальная черта (конвейер) в этом файле не используется -
rem на ней cmd спотыкается и обрывает работу.

setlocal

set "DIR=%~dp0"
set "APPDIR=%DIR%tools\dbapp"
set "APP=%APPDIR%\main.py"

if not exist "%APP%" goto :noapp

set "PYW="

rem 1. Окружение, которое ставит сама программа.
call :try_dir "%USERPROFILE%\.workbuddy-ai\binaries\python\envs\dbapp\Scripts"

rem 2. Любые другие окружения рядом с профилем.
for /d %%D in ("%USERPROFILE%\.workbuddy-ai\binaries\python\envs\*") do call :try_dir "%%~fD\Scripts"

rem 3. Python, установленный в систему обычным установщиком.
for /d %%D in ("%LOCALAPPDATA%\Programs\Python\Python*") do call :try_dir "%%~fD"
for /d %%D in ("%ProgramFiles%\Python*") do call :try_dir "%%~fD"

rem 4. Python, прописанный в PATH.
for %%P in (python.exe) do call :try_exe "%%~$PATH:P"
for %%P in (pythonw.exe) do call :try_exe "%%~$PATH:P"

if not defined PYW goto :nopython

pushd "%APPDIR%"
start "" "%PYW%" -X utf8 "%APP%"
popd
exit /b 0

:try_dir
rem Проверяет Python в папке: сначала обычный, потом оконный.
if defined PYW exit /b
if "%~1"=="" exit /b
call :try_exe "%~1\python.exe"
call :try_exe "%~1\pythonw.exe"
exit /b

:try_exe
rem Годится ли этот файл Python: есть ли в нём окна PyQt6.
rem Заглушку из Microsoft Store пропускаем: она стоит в папке WindowsApps,
rem ничего не делает и при этом отвечает, что всё хорошо.
if defined PYW exit /b
if "%~1"=="" exit /b
if not exist "%~1" exit /b
set "CAND=%~1"
set "SHORT=%CAND%"
call set "SHORT=%%SHORT:WindowsApps=%%"
if not "%SHORT%"=="%CAND%" exit /b
"%CAND%" -c "import PyQt6" >nul 2>&1
if errorlevel 1 exit /b
set "PYW=%CAND%"
if exist "%~dp1pythonw.exe" set "PYW=%~dp1pythonw.exe"
exit /b

:nopython
echo.
echo Не найден Python с окнами PyQt6 - без него окно не открыть.
echo.
echo Что делать: установите Python 3 с сайта python.org
echo (при установке отметьте галочку "Add Python to PATH"),
echo затем выполните в командной строке:
echo.
echo     python -m pip install PyQt6
echo.
echo После этого запустите этот файл снова.
echo.
pause
exit /b 1

:noapp
echo.
echo Не найден файл программы:
echo   %APP%
echo.
echo Проверьте, что папка tools\dbapp есть рядом с этим файлом.
echo.
pause
exit /b 1
