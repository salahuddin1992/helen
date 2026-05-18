; Helen-Rendezvous-Setup — production NSIS installer.
; Wraps the PyInstaller bundle for a NAT-traversal coordinator that
; runs on any LAN-internal host visible to all subnets it federates.
;
; Build with:
;   "<makensis>\makensis.exe" installer.nsi
; Output: Helen-Rendezvous-Setup-1.0.0.exe

!define APP_NAME      "Helen-Rendezvous"
!define APP_VERSION   "1.0.0"
!define APP_PUBLISHER "Helen Project"
!define APP_DESC      "Helen LAN NAT-traversal coordinator (internal use)"
!define APP_SVC_NAME  "HelenRendezvous"

!include "MUI2.nsh"
!include "LogicLib.nsh"

Name "${APP_NAME} ${APP_VERSION}"
OutFile "Helen-Rendezvous-Setup-${APP_VERSION}.exe"
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

Section "Helen-Rendezvous (required)" SecCore
    SectionIn RO
    SetOutPath "$INSTDIR"

    File /r "dist\Helen-Rendezvous\*.*"

    SetOutPath "$INSTDIR\bin\nssm"
    File /r "bin\nssm\*.*"

    SetOutPath "$INSTDIR"
    File "LICENSE.txt"
    File "installer-icon.ico"

    ; Generate a 64-hex token if one is not present.
    ; 3-tier escalation matches the Helen-Server installer:
    ;   1) PowerShell + RNGCryptoServiceProvider (preferred)
    ;   2) certutil hash of system entropy
    ;   3) cmd %RANDOM% chain (lower entropy but per-install unique)
    ; If all three fail we write a deliberate placeholder; the
    ; Helen-Rendezvous lifespan startup refuses to run with that
    ; exact value (mirroring _WEAK_TOKENS in Helen-Router).
    IfFileExists "$INSTDIR\.env" envExists envMake
    envMake:
        FileOpen $0 "$INSTDIR\.env" w
        FileWrite $0 "HELEN_RENDEZVOUS_TOKEN="

        ; Tier 1: PowerShell RNG
        nsExec::ExecToStack 'powershell -NoProfile -Command "$$b=New-Object byte[] 32; [System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($$b); -join ($$b | ForEach-Object {$$_.ToString(\"x2\")})"'
        Pop $1
        Pop $2
        ${If} $1 == 0
            FileWrite $0 "$2"
        ${Else}
            ; Tier 2: certutil -hashfile of randomized temp content
            GetTempFileName $3
            nsExec::ExecToStack 'cmd /c "echo %RANDOM%-%TIME%-%DATE%-%COMPUTERNAME%-%RANDOM%-%RANDOM%-%RANDOM% > $3 & certutil -hashfile $3 SHA256 | findstr /v hash | findstr /v CertUtil"'
            Pop $4
            Pop $5
            Delete $3
            ${If} $4 == 0
                FileWrite $0 "$5"
            ${Else}
                ; Tier 3: cmd %RANDOM% chain (per-install unique)
                nsExec::ExecToStack 'cmd /c "echo %RANDOM%%RANDOM%%RANDOM%%RANDOM%%RANDOM%%RANDOM%%RANDOM%%RANDOM%%TIME::=%%DATE:/=%"'
                Pop $6
                Pop $7
                ${If} $6 == 0
                    FileWrite $0 "$7"
                ${Else}
                    ; Last-ditch placeholder — refuses-to-start sentinel
                    FileWrite $0 "REPLACE_ME_BEFORE_RUNNING_HELEN_RENDEZVOUS_64_chars_long_xxxxxxxxxx"
                ${EndIf}
            ${EndIf}
        ${EndIf}
        FileWrite $0 "$\r$\n"
        FileWrite $0 "HELEN_RENDEZVOUS_HOST=0.0.0.0$\r$\n"
        FileWrite $0 "HELEN_RENDEZVOUS_PORT=9090$\r$\n"
        FileWrite $0 "HELEN_RELAY_BACKEND_PORT=9101$\r$\n"
        FileWrite $0 "HELEN_RELAY_FRONTEND_PORT=9102$\r$\n"
        FileClose $0

        ; Lock .env to Administrators + SYSTEM only.
        nsExec::ExecToLog 'icacls "$INSTDIR\.env" /inheritance:r /grant:r "*S-1-5-32-544:F" /grant:r "*S-1-5-18:F"'
    envExists:

    ; Start menu
    CreateDirectory "$SMPROGRAMS\${APP_NAME}"
    CreateShortcut "$SMPROGRAMS\${APP_NAME}\Start ${APP_NAME}.lnk" \
        "$INSTDIR\Helen-Rendezvous.exe" "" "$INSTDIR\installer-icon.ico"
    CreateShortcut "$SMPROGRAMS\${APP_NAME}\Edit configuration.lnk" \
        "notepad.exe" "$INSTDIR\.env"
    CreateShortcut "$SMPROGRAMS\${APP_NAME}\Uninstall ${APP_NAME}.lnk" \
        "$INSTDIR\uninstall.exe"

    WriteUninstaller "$INSTDIR\uninstall.exe"

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
SectionEnd

Section "Install as Windows service (auto-start at boot)" SecService
    DetailPrint "Registering ${APP_SVC_NAME} as a Windows service via NSSM..."

    nsExec::ExecToLog '"$INSTDIR\bin\nssm\nssm.exe" stop ${APP_SVC_NAME}'
    nsExec::ExecToLog '"$INSTDIR\bin\nssm\nssm.exe" remove ${APP_SVC_NAME} confirm'

    nsExec::ExecToLog '"$INSTDIR\bin\nssm\nssm.exe" install ${APP_SVC_NAME} "$INSTDIR\Helen-Rendezvous.exe"'
    nsExec::ExecToLog '"$INSTDIR\bin\nssm\nssm.exe" set ${APP_SVC_NAME} AppDirectory "$INSTDIR"'
    nsExec::ExecToLog '"$INSTDIR\bin\nssm\nssm.exe" set ${APP_SVC_NAME} DisplayName "Helen Rendezvous"'
    nsExec::ExecToLog '"$INSTDIR\bin\nssm\nssm.exe" set ${APP_SVC_NAME} Description "Helen NAT-traversal coordinator (HTTP 9090, relay 9101/9102)"'
    nsExec::ExecToLog '"$INSTDIR\bin\nssm\nssm.exe" set ${APP_SVC_NAME} Start SERVICE_AUTO_START'
    nsExec::ExecToLog '"$INSTDIR\bin\nssm\nssm.exe" set ${APP_SVC_NAME} AppStdout "$INSTDIR\service.out.log"'
    nsExec::ExecToLog '"$INSTDIR\bin\nssm\nssm.exe" set ${APP_SVC_NAME} AppStderr "$INSTDIR\service.err.log"'
    nsExec::ExecToLog '"$INSTDIR\bin\nssm\nssm.exe" set ${APP_SVC_NAME} AppRotateFiles 1'
    nsExec::ExecToLog '"$INSTDIR\bin\nssm\nssm.exe" set ${APP_SVC_NAME} AppRotateBytes 10485760'
    nsExec::ExecToLog '"$INSTDIR\bin\nssm\nssm.exe" set ${APP_SVC_NAME} AppRestartDelay 5000'

    nsExec::ExecToLog '"$INSTDIR\bin\nssm\nssm.exe" start ${APP_SVC_NAME}'
SectionEnd

Section "Add Windows Firewall rules (LAN only)" SecFirewall
    DetailPrint "Adding firewall rules for Rendezvous (9090, 9101, 9102)..."

    nsExec::ExecToLog 'netsh advfirewall firewall delete rule name="Helen-Rendezvous HTTP"'
    nsExec::ExecToLog 'netsh advfirewall firewall delete rule name="Helen-Rendezvous Relay backend"'
    nsExec::ExecToLog 'netsh advfirewall firewall delete rule name="Helen-Rendezvous Relay frontend"'

    ; LAN-only — RFC1918 + loopback
    nsExec::ExecToLog 'netsh advfirewall firewall add rule name="Helen-Rendezvous HTTP" dir=in action=allow protocol=TCP localport=9090 remoteip=127.0.0.1,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,169.254.0.0/16 profile=any'
    nsExec::ExecToLog 'netsh advfirewall firewall add rule name="Helen-Rendezvous Relay backend" dir=in action=allow protocol=TCP localport=9101 remoteip=127.0.0.1,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,169.254.0.0/16 profile=any'
    nsExec::ExecToLog 'netsh advfirewall firewall add rule name="Helen-Rendezvous Relay frontend" dir=in action=allow protocol=TCP localport=9102 remoteip=127.0.0.1,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,169.254.0.0/16 profile=any'
SectionEnd

LangString DESC_SecCore     ${LANG_ENGLISH} "Core rendezvous files (required)."
LangString DESC_SecService  ${LANG_ENGLISH} "Register as a Windows service so the rendezvous starts at boot, before any user logs in."
LangString DESC_SecFirewall ${LANG_ENGLISH} "Allow inbound TCP 9090 / 9101 / 9102 from private LAN ranges only (RFC 1918)."

!insertmacro MUI_FUNCTION_DESCRIPTION_BEGIN
    !insertmacro MUI_DESCRIPTION_TEXT ${SecCore}     $(DESC_SecCore)
    !insertmacro MUI_DESCRIPTION_TEXT ${SecService}  $(DESC_SecService)
    !insertmacro MUI_DESCRIPTION_TEXT ${SecFirewall} $(DESC_SecFirewall)
!insertmacro MUI_FUNCTION_DESCRIPTION_END

Section "Uninstall"
    DetailPrint "Stopping service..."
    nsExec::ExecToLog '"$INSTDIR\bin\nssm\nssm.exe" stop ${APP_SVC_NAME}'
    nsExec::ExecToLog '"$INSTDIR\bin\nssm\nssm.exe" remove ${APP_SVC_NAME} confirm'

    DetailPrint "Removing firewall rules..."
    nsExec::ExecToLog 'netsh advfirewall firewall delete rule name="Helen-Rendezvous HTTP"'
    nsExec::ExecToLog 'netsh advfirewall firewall delete rule name="Helen-Rendezvous Relay backend"'
    nsExec::ExecToLog 'netsh advfirewall firewall delete rule name="Helen-Rendezvous Relay frontend"'

    Delete "$SMPROGRAMS\${APP_NAME}\Start ${APP_NAME}.lnk"
    Delete "$SMPROGRAMS\${APP_NAME}\Edit configuration.lnk"
    Delete "$SMPROGRAMS\${APP_NAME}\Uninstall ${APP_NAME}.lnk"
    RMDir  "$SMPROGRAMS\${APP_NAME}"

    RMDir /r "$INSTDIR\bin"
    RMDir /r "$INSTDIR\_internal"
    Delete "$INSTDIR\Helen-Rendezvous.exe"
    Delete "$INSTDIR\LICENSE.txt"
    Delete "$INSTDIR\installer-icon.ico"
    Delete "$INSTDIR\uninstall.exe"
    RMDir "$INSTDIR"

    DeleteRegKey HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APP_NAME}"
SectionEnd
