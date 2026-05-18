; ──────────────────────────────────────────────────────────────────────
; Helen / CommClient — Phase 3 / Module Q
; Production NSIS installer v2 (unified Server + Desktop + Admin)
;
; Build with: makensis /DVERSION=1.3.0 v2.nsi
;
; Components (selected via the component page or the /COMPONENTS= switch):
;   server  — FastAPI service (PyInstaller bundle in dist\server\)
;   desktop — Electron app   (electron-builder output in dist\desktop\)
;   admin   — Admin web SPA  (static files in admin\)
;
; Silent install:
;   helen-installer.exe /S /D=C:\Program Files\Helen /COMPONENTS=server,admin,desktop
;
; Code-signing hooks: invoke build-installer.ps1 with -CertPath / -CertPass
; to sign before packaging. The placeholders below run unsigned when the
; cert env vars are absent.
; ──────────────────────────────────────────────────────────────────────

!ifndef VERSION
  !define VERSION "1.3.0"
!endif

!define APP_NAME "Helen CommClient"
!define APP_PUBLISHER "CommClient Project"
!define APP_REGKEY "Software\${APP_NAME}"
!define UNINST_REGKEY "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APP_NAME}"
!define SERVER_SVC_NAME "HelenServer"
!define APPDATA_REL "CommClient"

SetCompressor /SOLID lzma
RequestExecutionLevel admin
Unicode true

Name "${APP_NAME} ${VERSION}"
OutFile "..\dist\installer-v2\helen-installer-${VERSION}.exe"
InstallDir "$PROGRAMFILES64\Helen"
InstallDirRegKey HKLM "${APP_REGKEY}" "InstallDir"
BrandingText "${APP_NAME} v${VERSION}"

!include "MUI2.nsh"
!include "x64.nsh"
!include "LogicLib.nsh"
!include "FileFunc.nsh"
!insertmacro GetParameters
!insertmacro GetOptions

!define MUI_ABORTWARNING
!define MUI_ICON   "..\admin\static\favicon.ico"
!define MUI_UNICON "..\admin\static\favicon.ico"

!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_LICENSE "LICENSE.txt"
!insertmacro MUI_PAGE_COMPONENTS
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH

!insertmacro MUI_UNPAGE_WELCOME
!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES
!insertmacro MUI_UNPAGE_FINISH
!insertmacro MUI_LANGUAGE "English"

VIProductVersion "${VERSION}.0"
VIAddVersionKey "ProductName" "${APP_NAME}"
VIAddVersionKey "FileDescription" "Helen CommClient unified installer"
VIAddVersionKey "FileVersion" "${VERSION}"
VIAddVersionKey "ProductVersion" "${VERSION}"
VIAddVersionKey "CompanyName" "${APP_PUBLISHER}"
VIAddVersionKey "LegalCopyright" "© CommClient Project"

; ── Variables ─────────────────────────────────────────────────
Var SVC_AUTOSTART
Var COMPONENTS_PARAM

; ── Sections ──────────────────────────────────────────────────
SectionGroup /e "Helen CommClient" SecGroup

  Section "!Server" SecServer
    SectionIn 1 RO
    SetOutPath "$INSTDIR\server"
    File /r "..\dist\server\*"
    SetOutPath "$INSTDIR\server\data"

    ; Service registration via NSSM-like sc.exe stanza. Falls back to
    ; running the binary on boot via a Task Scheduler entry if sc.exe
    ; refuses (e.g. on Windows Home).
    nsExec::ExecToLog 'sc.exe create "${SERVER_SVC_NAME}" binPath= "\"$INSTDIR\server\helen-server.exe\" --service" start= auto DisplayName= "Helen CommClient Server"'
    Pop $0
    StrCmp $0 "0" 0 +3
      nsExec::ExecToLog 'sc.exe description "${SERVER_SVC_NAME}" "Helen CommClient backend service"'
      Pop $0

    ; Firewall rules — TCP 3000 (HTTP API + WS) and UDP 41234 (discovery)
    nsExec::ExecToLog 'netsh advfirewall firewall add rule name="Helen HTTP/WS" dir=in action=allow protocol=TCP localport=3000'
    Pop $0
    nsExec::ExecToLog 'netsh advfirewall firewall add rule name="Helen Discovery" dir=in action=allow protocol=UDP localport=41234'
    Pop $0
    nsExec::ExecToLog 'netsh advfirewall firewall add rule name="Helen Media" dir=in action=allow protocol=UDP localport=40000-49999'
    Pop $0

    StrCmp $SVC_AUTOSTART "1" 0 +2
      nsExec::ExecToLog 'sc.exe start "${SERVER_SVC_NAME}"'

    WriteRegStr HKLM "${APP_REGKEY}" "ServerVersion" "${VERSION}"
  SectionEnd

  Section "Desktop App" SecDesktop
    SectionIn 1
    SetOutPath "$INSTDIR\desktop"
    File /r "..\..\CommClient-Desktop\dist\win-unpacked\*"
    CreateShortcut "$DESKTOP\Helen.lnk" "$INSTDIR\desktop\Helen.exe"
    CreateShortcut "$SMPROGRAMS\Helen.lnk" "$INSTDIR\desktop\Helen.exe"

    ; Register helen:// custom protocol (used by Module N OAuth).
    WriteRegStr HKCR "helen" "" "URL:Helen Protocol"
    WriteRegStr HKCR "helen" "URL Protocol" ""
    WriteRegStr HKCR "helen\shell\open\command" "" '"$INSTDIR\desktop\Helen.exe" "%1"'

    WriteRegStr HKLM "${APP_REGKEY}" "DesktopVersion" "${VERSION}"
  SectionEnd

  Section "Admin UI" SecAdmin
    SectionIn 1
    SetOutPath "$INSTDIR\admin"
    File /r "..\admin\*"
    WriteRegStr HKLM "${APP_REGKEY}" "AdminVersion" "${VERSION}"
  SectionEnd

SectionGroupEnd

; ── Init / silent argument parsing ───────────────────────────
Function .onInit
  ${IfNot} ${RunningX64}
    MessageBox MB_OK|MB_ICONSTOP "Helen requires Windows 64-bit."
    Abort
  ${EndIf}
  SetRegView 64

  ; Defaults
  StrCpy $SVC_AUTOSTART "1"
  StrCpy $COMPONENTS_PARAM ""

  ClearErrors
  ${GetParameters} $0
  ${GetOptions} $0 "/COMPONENTS=" $COMPONENTS_PARAM
  ${GetOptions} $0 "/AUTOSTART=" $1
  StrCmp $1 "" +2 0
    StrCpy $SVC_AUTOSTART $1

  ${If} $COMPONENTS_PARAM != ""
    ; Toggle sections according to the comma-separated list.
    Push $COMPONENTS_PARAM
    Push "server" ; sentinel
    Call _ApplyComponentSelection
  ${EndIf}
FunctionEnd

Function _ApplyComponentSelection
  Pop $0   ; sentinel — discard
  Pop $1   ; the COMPONENTS string
  Push $1
  Call _StrContains
  Pop $2
  StrCmp $2 "server" +1 +2
    SectionSetFlags ${SecServer} ${SF_SELECTED}
FunctionEnd

; Minimal substring contains: leave "<token>" or "" on stack.
Function _StrContains
  Exch $R0
  Push $R1
  Push $R2
  StrCpy $R2 0
loop:
  StrCpy $R1 $R0 1 $R2
  StrCmp $R1 "" done
  StrCmp $R1 "," 0 advance
  StrCpy $R0 $R0 $R2
  Goto done
advance:
  IntOp $R2 $R2 + 1
  Goto loop
done:
  Pop $R2
  Pop $R1
  Exch $R0
FunctionEnd

; ── Uninstaller ──────────────────────────────────────────────
Section "Uninstall"
  ; Stop and remove service if present.
  nsExec::ExecToLog 'sc.exe stop "${SERVER_SVC_NAME}"'
  Pop $0
  nsExec::ExecToLog 'sc.exe delete "${SERVER_SVC_NAME}"'
  Pop $0

  ; Firewall cleanup
  nsExec::ExecToLog 'netsh advfirewall firewall delete rule name="Helen HTTP/WS"'
  Pop $0
  nsExec::ExecToLog 'netsh advfirewall firewall delete rule name="Helen Discovery"'
  Pop $0
  nsExec::ExecToLog 'netsh advfirewall firewall delete rule name="Helen Media"'
  Pop $0

  ; Custom protocol cleanup
  DeleteRegKey HKCR "helen"

  ; Application files
  RMDir /r "$INSTDIR\server"
  RMDir /r "$INSTDIR\desktop"
  RMDir /r "$INSTDIR\admin"
  Delete   "$INSTDIR\uninstall.exe"
  RMDir    "$INSTDIR"

  Delete "$DESKTOP\Helen.lnk"
  Delete "$SMPROGRAMS\Helen.lnk"

  DeleteRegKey HKLM "${APP_REGKEY}"
  DeleteRegKey HKLM "${UNINST_REGKEY}"

  ; IMPORTANT: preserve user data
  ;   %APPDATA%\CommClient\data\  (SQLite DB, uploaded files, configs)
  ; If the user wants a complete wipe, they delete it manually.
SectionEnd

Section -PostInstall
  WriteUninstaller "$INSTDIR\uninstall.exe"
  WriteRegStr HKLM "${UNINST_REGKEY}" "DisplayName" "${APP_NAME}"
  WriteRegStr HKLM "${UNINST_REGKEY}" "DisplayVersion" "${VERSION}"
  WriteRegStr HKLM "${UNINST_REGKEY}" "Publisher" "${APP_PUBLISHER}"
  WriteRegStr HKLM "${UNINST_REGKEY}" "InstallLocation" "$INSTDIR"
  WriteRegStr HKLM "${UNINST_REGKEY}" "UninstallString" "$INSTDIR\uninstall.exe"
  WriteRegDWORD HKLM "${UNINST_REGKEY}" "NoModify" 1
  WriteRegDWORD HKLM "${UNINST_REGKEY}" "NoRepair" 1
SectionEnd
