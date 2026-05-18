; Helen-Router-Setup — production NSIS installer.
; Installs the LAN entry-point reverse proxy as a Windows service.

!define APP_NAME      "Helen-Router"
!define APP_VERSION   "1.0.0"
!define APP_PUBLISHER "Helen Project"
!define APP_DESC      "Helen LAN router / mandatory entry point"
!define APP_SVC_NAME  "HelenRouter"

!include "MUI2.nsh"
!include "LogicLib.nsh"

Name "${APP_NAME} ${APP_VERSION}"
OutFile "Helen-Router-Setup-${APP_VERSION}.exe"
InstallDir "$PROGRAMFILES64\${APP_NAME}"
RequestExecutionLevel admin
ShowInstDetails show
ShowUninstDetails show

!define MUI_ICON     "installer-icon.ico"
!define MUI_UNICON   "installer-icon.ico"
!define MUI_ABORTWARNING

!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_LICENSE  "LICENSE.txt"
!insertmacro MUI_PAGE_COMPONENTS
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH

!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES

!insertmacro MUI_LANGUAGE "English"

Section "Helen-Router (required)" SecCore
    SectionIn RO
    SetOutPath "$INSTDIR"
    File /r "dist\Helen-Router\*.*"

    SetOutPath "$INSTDIR\bin\nssm"
    File /r "bin\nssm\*.*"

    SetOutPath "$INSTDIR"
    File "LICENSE.txt"
    File "installer-icon.ico"

    IfFileExists "$INSTDIR\.env" envExists envMake
    envMake:
        FileOpen $0 "$INSTDIR\.env" w
        FileWrite $0 "HELEN_ROUTER_TOKEN="

        ; Tier 1: PowerShell + RNGCryptoServiceProvider (preferred)
        nsExec::ExecToStack 'powershell -NoProfile -Command "$$b=New-Object byte[] 32; [System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($$b); -join ($$b | ForEach-Object {$$_.ToString(\"x2\")})"'
        Pop $1
        Pop $2
        ${If} $1 == 0
            FileWrite $0 "$2"
        ${Else}
            ; Tier 2: certutil -hashfile of a temp file with system entropy
            GetTempFileName $3
            nsExec::ExecToStack 'cmd /c "echo %RANDOM%-%TIME%-%DATE%-%COMPUTERNAME%-%RANDOM%-%RANDOM%-%RANDOM% > $3 & certutil -hashfile $3 SHA256 | findstr /v hash | findstr /v CertUtil"'
            Pop $4
            Pop $5
            Delete $3
            ${If} $4 == 0
                FileWrite $0 "$5"
            ${Else}
                ; Tier 3: cmd %RANDOM% chain — last-ditch, lower entropy
                ; but still per-install unique (clock + PID).
                nsExec::ExecToStack 'cmd /c "echo %RANDOM%%RANDOM%%RANDOM%%RANDOM%%RANDOM%%RANDOM%%RANDOM%%RANDOM%%TIME::=%%DATE:/=%"'
                Pop $6
                Pop $7
                ${If} $6 == 0
                    FileWrite $0 "$7"
                ${Else}
                    ; Absolute last-resort placeholder — server's
                    ; HELEN_ROUTER_TOKEN check rejects this exact
                    ; string so the deployment refuses to start
                    ; rather than running with a known-leaked token.
                    FileWrite $0 "REPLACE_ME_BEFORE_RUNNING_HELEN_ROUTER_64_chars_long_xxxxxxxxxx"
                ${EndIf}
            ${EndIf}
        ${EndIf}
        FileWrite $0 "$\r$\n"
        FileWrite $0 "HELEN_ROUTER_HOST=0.0.0.0$\r$\n"
        FileWrite $0 "HELEN_ROUTER_PORT=8080$\r$\n"
        FileClose $0

        ; Lock .env so only Administrators can read the token.
        nsExec::ExecToLog 'icacls "$INSTDIR\.env" /inheritance:r /grant:r "*S-1-5-32-544:F" /grant:r "*S-1-5-18:F"'
    envExists:

    CreateDirectory "$SMPROGRAMS\${APP_NAME}"
    CreateShortcut "$SMPROGRAMS\${APP_NAME}\Start ${APP_NAME}.lnk" \
        "$INSTDIR\Helen-Router.exe" "" "$INSTDIR\installer-icon.ico"
    CreateShortcut "$SMPROGRAMS\${APP_NAME}\Edit configuration.lnk" \
        "notepad.exe" "$INSTDIR\.env"
    CreateShortcut "$SMPROGRAMS\${APP_NAME}\Uninstall ${APP_NAME}.lnk" \
        "$INSTDIR\uninstall.exe"

    WriteUninstaller "$INSTDIR\uninstall.exe"

    WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APP_NAME}" "DisplayName" "${APP_NAME} ${APP_VERSION}"
    WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APP_NAME}" "UninstallString" "$\"$INSTDIR\uninstall.exe$\""
    WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APP_NAME}" "Publisher" "${APP_PUBLISHER}"
    WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APP_NAME}" "DisplayVersion" "${APP_VERSION}"
    WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APP_NAME}" "DisplayIcon" "$INSTDIR\installer-icon.ico"
    WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APP_NAME}" "InstallLocation" "$INSTDIR"
SectionEnd

Section "Install as Windows service (auto-start at boot)" SecService
    DetailPrint "Registering ${APP_SVC_NAME} as a Windows service via NSSM..."
    nsExec::ExecToLog '"$INSTDIR\bin\nssm\nssm.exe" stop ${APP_SVC_NAME}'
    nsExec::ExecToLog '"$INSTDIR\bin\nssm\nssm.exe" remove ${APP_SVC_NAME} confirm'
    nsExec::ExecToLog '"$INSTDIR\bin\nssm\nssm.exe" install ${APP_SVC_NAME} "$INSTDIR\Helen-Router.exe"'
    nsExec::ExecToLog '"$INSTDIR\bin\nssm\nssm.exe" set ${APP_SVC_NAME} AppDirectory "$INSTDIR"'
    nsExec::ExecToLog '"$INSTDIR\bin\nssm\nssm.exe" set ${APP_SVC_NAME} DisplayName "Helen Router"'
    nsExec::ExecToLog '"$INSTDIR\bin\nssm\nssm.exe" set ${APP_SVC_NAME} Description "Helen LAN router (HTTP 8080)"'
    nsExec::ExecToLog '"$INSTDIR\bin\nssm\nssm.exe" set ${APP_SVC_NAME} Start SERVICE_AUTO_START'
    nsExec::ExecToLog '"$INSTDIR\bin\nssm\nssm.exe" set ${APP_SVC_NAME} AppStdout "$INSTDIR\service.out.log"'
    nsExec::ExecToLog '"$INSTDIR\bin\nssm\nssm.exe" set ${APP_SVC_NAME} AppStderr "$INSTDIR\service.err.log"'
    nsExec::ExecToLog '"$INSTDIR\bin\nssm\nssm.exe" set ${APP_SVC_NAME} AppRotateFiles 1'
    nsExec::ExecToLog '"$INSTDIR\bin\nssm\nssm.exe" set ${APP_SVC_NAME} AppRotateBytes 10485760'
    nsExec::ExecToLog '"$INSTDIR\bin\nssm\nssm.exe" set ${APP_SVC_NAME} AppRestartDelay 5000'
    nsExec::ExecToLog '"$INSTDIR\bin\nssm\nssm.exe" start ${APP_SVC_NAME}'
SectionEnd

Section "Add Windows Firewall rule (LAN only)" SecFirewall
    DetailPrint "Adding firewall rule for Helen-Router (8080)..."
    nsExec::ExecToLog 'netsh advfirewall firewall delete rule name="Helen-Router HTTP"'
    nsExec::ExecToLog 'netsh advfirewall firewall add rule name="Helen-Router HTTP" dir=in action=allow protocol=TCP localport=8080 remoteip=127.0.0.1,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,169.254.0.0/16 profile=any'
SectionEnd

LangString DESC_SecCore     ${LANG_ENGLISH} "Core router files (required)."
LangString DESC_SecService  ${LANG_ENGLISH} "Register as a Windows service so the router starts at boot."
LangString DESC_SecFirewall ${LANG_ENGLISH} "Allow inbound TCP 8080 from RFC1918 sources only."

!insertmacro MUI_FUNCTION_DESCRIPTION_BEGIN
    !insertmacro MUI_DESCRIPTION_TEXT ${SecCore}     $(DESC_SecCore)
    !insertmacro MUI_DESCRIPTION_TEXT ${SecService}  $(DESC_SecService)
    !insertmacro MUI_DESCRIPTION_TEXT ${SecFirewall} $(DESC_SecFirewall)
!insertmacro MUI_FUNCTION_DESCRIPTION_END

Section "Uninstall"
    nsExec::ExecToLog '"$INSTDIR\bin\nssm\nssm.exe" stop ${APP_SVC_NAME}'
    nsExec::ExecToLog '"$INSTDIR\bin\nssm\nssm.exe" remove ${APP_SVC_NAME} confirm'
    nsExec::ExecToLog 'netsh advfirewall firewall delete rule name="Helen-Router HTTP"'

    Delete "$SMPROGRAMS\${APP_NAME}\Start ${APP_NAME}.lnk"
    Delete "$SMPROGRAMS\${APP_NAME}\Edit configuration.lnk"
    Delete "$SMPROGRAMS\${APP_NAME}\Uninstall ${APP_NAME}.lnk"
    RMDir  "$SMPROGRAMS\${APP_NAME}"

    RMDir /r "$INSTDIR\bin"
    RMDir /r "$INSTDIR\_internal"
    Delete "$INSTDIR\Helen-Router.exe"
    Delete "$INSTDIR\LICENSE.txt"
    Delete "$INSTDIR\installer-icon.ico"
    Delete "$INSTDIR\uninstall.exe"
    RMDir "$INSTDIR"

    DeleteRegKey HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APP_NAME}"
SectionEnd
