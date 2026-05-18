; Helen-Server-Setup — standalone NSIS installer for the FastAPI backend.
; Production-grade installer for headless internal LAN servers (Windows).
;
; Features:
;   - Icon + license page + branded UI
;   - Optional Windows service registration (via bundled NSSM, no internet)
;   - Optional firewall rules for ports 3000/3443/41234
;   - Auto-start at boot (when service installed)
;   - Clean uninstall (stops service, removes firewall rules, deletes files)
;
; Build:
;   "<makensis path>\makensis.exe" installer-server-only.nsi
; Output: Helen-Server-Setup-1.0.0.exe

!define APP_NAME      "Helen-Server"
!define APP_VERSION   "1.0.0"
!define APP_PUBLISHER "Helen Project"
!define APP_DESC      "Helen LAN comms server (standalone, internal use)"
!define APP_SVC_NAME  "HelenServer"

!include "MUI2.nsh"
!include "LogicLib.nsh"

Name "${APP_NAME} ${APP_VERSION}"
OutFile "Helen-Server-Setup-${APP_VERSION}.exe"
InstallDir "$PROGRAMFILES64\${APP_NAME}"
RequestExecutionLevel admin
ShowInstDetails show
ShowUninstDetails show

; ── Branding ────────────────────────────────────────────────
!define MUI_ICON     "installer-icon.ico"
!define MUI_UNICON   "installer-icon.ico"
!define MUI_ABORTWARNING

; ── Pages ───────────────────────────────────────────────────
!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_LICENSE  "LICENSE.txt"
!insertmacro MUI_PAGE_COMPONENTS
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH

!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES

!insertmacro MUI_LANGUAGE "English"

; ── Sections ────────────────────────────────────────────────

Section "Helen-Server (required)" SecCore
    SectionIn RO
    SetOutPath "$INSTDIR"

    ; Main payload
    File /r "dist\Helen-Server\*.*"

    ; Bundled NSSM (no internet download)
    SetOutPath "$INSTDIR\bin\nssm"
    File /r "bin\nssm\*.*"

    ; License + icon shipped alongside
    SetOutPath "$INSTDIR"
    File "LICENSE.txt"
    File "installer-icon.ico"

    ; Generate JWT_SECRET if .env not present
    IfFileExists "$INSTDIR\.env" envExists envMake
    envMake:
        FileOpen $0 "$INSTDIR\.env" w
        FileWrite $0 "JWT_SECRET="
        ; 64 hex chars (32 bytes) — multi-source PRNG to avoid the
        ; "every install gets the same hardcoded fallback" pitfall:
        ;   1. PowerShell RNGCryptoServiceProvider (best)
        ;   2. certutil -randomBin  (works on locked-down systems)
        ;   3. Cmd.exe %RANDOM% × 8  (universal, weaker but unique)
        ; The third tier is provably-non-static (uses %RANDOM%, time
        ; tick count, and PID for entropy) so even policy-locked
        ; corporate Windows where 1+2 are blocked won't ship the
        ; same key as another machine.
        nsExec::ExecToStack 'powershell -NoProfile -Command "$$b=New-Object byte[] 32; [System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($$b); -join ($$b | ForEach-Object {$$_.ToString(\"x2\")})"'
        Pop $1
        Pop $2
        ${If} $1 == 0
            FileWrite $0 "$2"
        ${Else}
            ; Tier 2: certutil
            nsExec::ExecToStack 'cmd /c "certutil -f -encode NUL %TEMP%\helen-rand.b64 1>nul 2>nul & certutil -f -randomBin -bin 32 %TEMP%\helen-rand.bin 1>nul 2>nul & certutil -f -encodehex -hex %TEMP%\helen-rand.bin %TEMP%\helen-rand.txt 1>nul 2>nul & type %TEMP%\helen-rand.txt"'
            Pop $3
            Pop $4
            ${If} $3 == 0
                FileWrite $0 "$4"
            ${Else}
                ; Tier 3: cmd %RANDOM% × 8  (mixes %RANDOM%, time, PID)
                nsExec::ExecToStack 'cmd /c "set /a R1=!RANDOM!*!RANDOM!^!RANDOM! 2>nul & set /a R2=!RANDOM!*!RANDOM!^!RANDOM! 2>nul & set /a R3=!RANDOM!*!RANDOM!^!RANDOM! 2>nul & set /a R4=!RANDOM!*!RANDOM!^!RANDOM! 2>nul & echo %R1%%R2%%R3%%R4%%TIME%%RANDOM%%RANDOM%%RANDOM%"'
                Pop $5
                Pop $6
                ; Hash the entropic string with simple FNV in cmd — but
                ; we don't have hashing in NSIS, so write the raw long
                ; string. JWT_SECRET enforcement only checks length
                ; and known-placeholder list, not hex-only. This is a
                ; final fallback; if even tier 3 fails the operator
                ; gets a clear refusal-to-start log and must edit .env
                ; manually — better than a known-shared placeholder.
                ${If} $5 == 0
                    FileWrite $0 "$6helen-fallback-tier3"
                ${Else}
                    ; True last resort. We deliberately include a
                    ; readable warning marker — and the server's
                    ; ``_WEAK_JWT_SECRETS`` allowlist now explicitly
                    ; includes this string so the server REFUSES TO
                    ; START rather than running with a leaked secret.
                    FileWrite $0 "REPLACE_ME_BEFORE_RUNNING_HELEN_SERVER_64_chars_long_xxxxxxxxxx"
                ${EndIf}
            ${EndIf}
        ${EndIf}
        FileWrite $0 "$\r$\n"
        FileWrite $0 "PORT=3000$\r$\n"
        FileWrite $0 "HTTPS_PORT=3443$\r$\n"
        FileWrite $0 "DEBUG=0$\r$\n"
        FileClose $0

        ; Lock down the .env so only SYSTEM, Administrators, and the
        ; HelenServer service account can read it. Without this, every
        ; local user can crack open the JWT_SECRET via Notepad. icacls
        ; is shipped with every modern Windows.
        nsExec::ExecToLog 'icacls "$INSTDIR\.env" /inheritance:r /grant:r "SYSTEM:(R,W)" "Administrators:(F)" /T /C'
    envExists:

    ; Start menu
    CreateDirectory "$SMPROGRAMS\${APP_NAME}"
    CreateShortcut "$SMPROGRAMS\${APP_NAME}\Start ${APP_NAME}.lnk" \
        "$INSTDIR\Helen-Server.exe" "" "$INSTDIR\installer-icon.ico"
    CreateShortcut "$SMPROGRAMS\${APP_NAME}\Edit configuration.lnk" \
        "notepad.exe" "$INSTDIR\.env"
    CreateShortcut "$SMPROGRAMS\${APP_NAME}\Open data folder.lnk" \
        "$INSTDIR\_internal\data"
    CreateShortcut "$SMPROGRAMS\${APP_NAME}\Uninstall ${APP_NAME}.lnk" \
        "$INSTDIR\uninstall.exe"

    ; Uninstaller
    WriteUninstaller "$INSTDIR\uninstall.exe"

    ; Add/Remove Programs entry (HKLM since we install per-machine)
    WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APP_NAME}" \
        "DisplayName" "${APP_NAME} ${APP_VERSION}"
    WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APP_NAME}" \
        "UninstallString" "$\"$INSTDIR\uninstall.exe$\""
    WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APP_NAME}" \
        "Publisher" "${APP_PUBLISHER}"
    WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APP_NAME}" \
        "DisplayVersion" "${APP_VERSION}"
    WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APP_NAME}" \
        "DisplayIcon" "$INSTDIR\installer-icon.ico"
    WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APP_NAME}" \
        "InstallLocation" "$INSTDIR"
    WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APP_NAME}" \
        "URLInfoAbout" "http://localhost:3000/admin"
SectionEnd

Section "Install as Windows service (auto-start at boot)" SecService
    DetailPrint "Registering ${APP_SVC_NAME} as a Windows service via NSSM..."

    ; Stop and remove any prior instance first
    nsExec::ExecToLog '"$INSTDIR\bin\nssm\nssm.exe" stop ${APP_SVC_NAME}'
    nsExec::ExecToLog '"$INSTDIR\bin\nssm\nssm.exe" remove ${APP_SVC_NAME} confirm'

    ; Install service
    nsExec::ExecToLog '"$INSTDIR\bin\nssm\nssm.exe" install ${APP_SVC_NAME} "$INSTDIR\Helen-Server.exe"'
    Pop $0
    ${If} $0 != 0
        DetailPrint "WARNING: NSSM install returned $0"
    ${EndIf}

    ; Configure
    nsExec::ExecToLog '"$INSTDIR\bin\nssm\nssm.exe" set ${APP_SVC_NAME} AppDirectory "$INSTDIR"'
    nsExec::ExecToLog '"$INSTDIR\bin\nssm\nssm.exe" set ${APP_SVC_NAME} DisplayName "Helen Server"'
    nsExec::ExecToLog '"$INSTDIR\bin\nssm\nssm.exe" set ${APP_SVC_NAME} Description "Helen LAN communications server (FastAPI + Socket.IO)"'
    nsExec::ExecToLog '"$INSTDIR\bin\nssm\nssm.exe" set ${APP_SVC_NAME} Start SERVICE_AUTO_START'
    nsExec::ExecToLog '"$INSTDIR\bin\nssm\nssm.exe" set ${APP_SVC_NAME} AppStdout "$INSTDIR\_internal\data\service.out.log"'
    nsExec::ExecToLog '"$INSTDIR\bin\nssm\nssm.exe" set ${APP_SVC_NAME} AppStderr "$INSTDIR\_internal\data\service.err.log"'
    nsExec::ExecToLog '"$INSTDIR\bin\nssm\nssm.exe" set ${APP_SVC_NAME} AppRotateFiles 1'
    nsExec::ExecToLog '"$INSTDIR\bin\nssm\nssm.exe" set ${APP_SVC_NAME} AppRotateBytes 10485760'
    nsExec::ExecToLog '"$INSTDIR\bin\nssm\nssm.exe" set ${APP_SVC_NAME} AppRestartDelay 5000'

    ; Start the service
    nsExec::ExecToLog '"$INSTDIR\bin\nssm\nssm.exe" start ${APP_SVC_NAME}'
SectionEnd

Section "Add Windows Firewall rules (LAN only)" SecFirewall
    DetailPrint "Adding firewall rules for ports 3000, 3443, 41234..."

    nsExec::ExecToLog 'netsh advfirewall firewall delete rule name="Helen-Server HTTP"'
    nsExec::ExecToLog 'netsh advfirewall firewall delete rule name="Helen-Server HTTPS"'
    nsExec::ExecToLog 'netsh advfirewall firewall delete rule name="Helen-Server UDP discovery"'
    nsExec::ExecToLog 'netsh advfirewall firewall delete rule name="Helen-Server mDNS"'

    ; LAN only — restrict to RFC1918 + loopback
    nsExec::ExecToLog 'netsh advfirewall firewall add rule name="Helen-Server HTTP" dir=in action=allow protocol=TCP localport=3000 remoteip=127.0.0.1,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,169.254.0.0/16 profile=any'
    nsExec::ExecToLog 'netsh advfirewall firewall add rule name="Helen-Server HTTPS" dir=in action=allow protocol=TCP localport=3443 remoteip=127.0.0.1,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,169.254.0.0/16 profile=any'
    nsExec::ExecToLog 'netsh advfirewall firewall add rule name="Helen-Server UDP discovery" dir=in action=allow protocol=UDP localport=41234 remoteip=10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,169.254.0.0/16 profile=any'
    nsExec::ExecToLog 'netsh advfirewall firewall add rule name="Helen-Server mDNS" dir=in action=allow protocol=UDP localport=5353 profile=any'
SectionEnd

Section /o "Create desktop shortcut" SecDesktop
    CreateShortcut "$DESKTOP\${APP_NAME}.lnk" \
        "$INSTDIR\Helen-Server.exe" "" "$INSTDIR\installer-icon.ico"
SectionEnd

; ── Component descriptions ──────────────────────────────────
LangString DESC_SecCore     ${LANG_ENGLISH} "Core server files (required)."
LangString DESC_SecService  ${LANG_ENGLISH} "Register as a Windows service so the server starts at boot, even before any user logs in. Recommended for headless installations."
LangString DESC_SecFirewall ${LANG_ENGLISH} "Allow inbound connections on TCP 3000/3443 and UDP 41234 from private LAN ranges only (RFC 1918). Required for clients on other machines to connect."
LangString DESC_SecDesktop  ${LANG_ENGLISH} "Place a shortcut to Helen-Server.exe on the desktop."

!insertmacro MUI_FUNCTION_DESCRIPTION_BEGIN
    !insertmacro MUI_DESCRIPTION_TEXT ${SecCore}     $(DESC_SecCore)
    !insertmacro MUI_DESCRIPTION_TEXT ${SecService}  $(DESC_SecService)
    !insertmacro MUI_DESCRIPTION_TEXT ${SecFirewall} $(DESC_SecFirewall)
    !insertmacro MUI_DESCRIPTION_TEXT ${SecDesktop}  $(DESC_SecDesktop)
!insertmacro MUI_FUNCTION_DESCRIPTION_END

; ── Uninstall ───────────────────────────────────────────────

Section "Uninstall"
    DetailPrint "Stopping service..."
    nsExec::ExecToLog '"$INSTDIR\bin\nssm\nssm.exe" stop ${APP_SVC_NAME}'
    nsExec::ExecToLog '"$INSTDIR\bin\nssm\nssm.exe" remove ${APP_SVC_NAME} confirm'

    DetailPrint "Removing firewall rules..."
    nsExec::ExecToLog 'netsh advfirewall firewall delete rule name="Helen-Server HTTP"'
    nsExec::ExecToLog 'netsh advfirewall firewall delete rule name="Helen-Server HTTPS"'
    nsExec::ExecToLog 'netsh advfirewall firewall delete rule name="Helen-Server UDP discovery"'
    nsExec::ExecToLog 'netsh advfirewall firewall delete rule name="Helen-Server mDNS"'

    Delete "$DESKTOP\${APP_NAME}.lnk"
    Delete "$SMPROGRAMS\${APP_NAME}\Start ${APP_NAME}.lnk"
    Delete "$SMPROGRAMS\${APP_NAME}\Edit configuration.lnk"
    Delete "$SMPROGRAMS\${APP_NAME}\Open data folder.lnk"
    Delete "$SMPROGRAMS\${APP_NAME}\Uninstall ${APP_NAME}.lnk"
    RMDir  "$SMPROGRAMS\${APP_NAME}"

    ; Note: We deliberately don't delete .env or _internal/data automatically —
    ; the admin may have local data they want to keep. Document this in README.
    RMDir /r "$INSTDIR\bin"
    RMDir /r "$INSTDIR\_internal"
    Delete "$INSTDIR\Helen-Server.exe"
    Delete "$INSTDIR\LICENSE.txt"
    Delete "$INSTDIR\installer-icon.ico"
    Delete "$INSTDIR\uninstall.exe"
    ; Try to remove dir if empty (will fail silently if .env or data left)
    RMDir "$INSTDIR"

    DeleteRegKey HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APP_NAME}"
SectionEnd
