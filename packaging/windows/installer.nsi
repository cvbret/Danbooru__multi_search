Unicode true
RequestExecutionLevel user
ManifestDPIAware true

!include "FileFunc.nsh"
!include "LogicLib.nsh"
!include "MUI2.nsh"
!include "Win\WinNT.nsh"

!ifndef APP_SOURCE
  !error "APP_SOURCE must point to the verified PyInstaller onedir directory."
!endif
!ifndef OUTPUT_DIR
  !error "OUTPUT_DIR must point to the verified installer output directory."
!endif

!define PRODUCT_VERSION "0.1.0"
!define PRODUCT_NAME "Danbooru Multi Search"
!define PRODUCT_PUBLISHER "Danbooru Multi Search Contributors"
!define PRODUCT_EXE "DanbooruMultiSearch.exe"
!define PRODUCT_KEY "Software\DanbooruMultiSearch"
!define UNINSTALL_KEY "Software\Microsoft\Windows\CurrentVersion\Uninstall\DanbooruMultiSearch"
!define START_MENU_DIR "Danbooru Multi Search"
!define START_MENU_LINK "Danbooru Multi Search.lnk"
!define MARKER_FILE ".danbooru-multi-search-install"
!define MARKER_VALUE "DanbooruMultiSearch|0.1.0|windows-amd64"
!define BUILD_OWNER_MARKER ".danbooru-multi-search-build-owner"
!define INSTALLER_FILE "DanbooruMultiSearch-0.1.0-win64-setup.exe"
!define INSTALL_ERROR_LOG "DanbooruMultiSearch-install-error.txt"

Name "${PRODUCT_NAME}"
OutFile "${OUTPUT_DIR}\${INSTALLER_FILE}"
VIProductVersion "0.1.0.0"
VIAddVersionKey /LANG=1033 "ProductName" "${PRODUCT_NAME}"
VIAddVersionKey /LANG=1033 "ProductVersion" "0.1.0.0"
VIAddVersionKey /LANG=1033 "FileDescription" "${PRODUCT_NAME} Installer"
VIAddVersionKey /LANG=1033 "FileVersion" "0.1.0.0"
VIAddVersionKey /LANG=1033 "CompanyName" "${PRODUCT_PUBLISHER}"
VIAddVersionKey /LANG=1033 "LegalCopyright" "${PRODUCT_PUBLISHER}"
InstallDir "$LOCALAPPDATA\Programs\DanbooruMultiSearch"
InstallDirRegKey HKCU "${PRODUCT_KEY}" "InstallLocation"
ShowInstDetails show
ShowUninstDetails show
SetCompressor /SOLID lzma

Var IsReinstall
Var NormalizedInstallDir
Var NativeArchitecture
Var MarkerContent
Var MarkerExtra
Var AppInstallLocation
Var AppDisplayVersion
Var UninstallLocation
Var UninstallDisplayVersion
Var DirectoryEntry
Var DirectoryHandle
Var FailureStage

!define MUI_ABORTWARNING
!define MUI_FINISHPAGE_RUN "$INSTDIR\app\${PRODUCT_EXE}"
!define MUI_FINISHPAGE_RUN_TEXT "Run ${PRODUCT_NAME}"
!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH
!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES
!insertmacro MUI_LANGUAGE "English"

Function FailInstall
  Exch $0
  IfSilent silent_failure interactive_failure
interactive_failure:
  MessageBox MB_OK|MB_ICONSTOP "$0"
  Goto failure_exit
silent_failure:
  ClearErrors
!ifdef INIT_PROBE_RESULT
  FileOpen $1 "${INIT_PROBE_RESULT}" w
!else
  FileOpen $1 "$TEMP\${INSTALL_ERROR_LOG}" w
!endif
  IfErrors failure_exit
  FileWriteUTF16LE /BOM $1 "stage=$FailureStage$\r$\n"
  FileWriteUTF16LE $1 "message=$0$\r$\n"
  FileClose $1
failure_exit:
  SetErrorLevel 2
  Abort
FunctionEnd

Function un.Fail
  Exch $0
  IfSilent +2 0
    MessageBox MB_OK|MB_ICONSTOP "$0"
  SetErrorLevel 2
  Abort
FunctionEnd

Function CheckNativeAmd64
  System::Alloc 64
  Pop $0
  StrCmp $0 0 architecture_error
  System::Call 'kernel32::GetNativeSystemInfo(p r0)'
  System::Call '*$0(&i2 .r1)'
  System::Free $0
  StrCpy $NativeArchitecture $1
  IntCmp $NativeArchitecture ${PROCESSOR_ARCHITECTURE_AMD64} architecture_ok architecture_error architecture_error

architecture_error:
  Push "${PRODUCT_NAME} requires native AMD64 Windows."
  Call FailInstall
architecture_ok:
FunctionEnd

Function un.CheckNativeAmd64
  System::Alloc 64
  Pop $0
  StrCmp $0 0 architecture_error
  System::Call 'kernel32::GetNativeSystemInfo(p r0)'
  System::Call '*$0(&i2 .r1)'
  System::Free $0
  IntCmp $1 ${PROCESSOR_ARCHITECTURE_AMD64} architecture_ok architecture_error architecture_error

architecture_error:
  Push "${PRODUCT_NAME} requires native AMD64 Windows."
  Call un.Fail
architecture_ok:
FunctionEnd

Function NormalizeInstallDirectory
  ${GetRoot} "$INSTDIR" $0
  StrCmp $0 "" invalid_path
  StrCpy $0 "$INSTDIR"
  System::Call 'kernel32::GetFullPathNameW(w r0, i ${NSIS_MAX_STRLEN}, w .r1, p 0) i .r2'
  StrCmp $2 0 invalid_path
  IntCmp $2 ${NSIS_MAX_STRLEN} invalid_path normalized_path invalid_path
normalized_path:
  StrCpy $NormalizedInstallDir $1
  StrCmp $NormalizedInstallDir "" invalid_path
  StrCpy $INSTDIR $NormalizedInstallDir
  Return
invalid_path:
  Push "The installation directory must be an absolute Windows path: $INSTDIR"
  Call FailInstall
FunctionEnd

Function un.NormalizeInstallDirectory
  ${GetRoot} "$INSTDIR" $0
  StrCmp $0 "" invalid_path
  StrCpy $0 "$INSTDIR"
  System::Call 'kernel32::GetFullPathNameW(w r0, i ${NSIS_MAX_STRLEN}, w .r1, p 0) i .r2'
  StrCmp $2 0 invalid_path
  IntCmp $2 ${NSIS_MAX_STRLEN} invalid_path normalized_path invalid_path
normalized_path:
  StrCpy $NormalizedInstallDir $1
  StrCmp $NormalizedInstallDir "" invalid_path
  StrCpy $INSTDIR $NormalizedInstallDir
  Return
invalid_path:
  Push "The uninstall directory is invalid."
  Call un.Fail
FunctionEnd

Function DirectoryHasEntries
  StrCpy $DirectoryEntry ""
  ClearErrors
  FindFirst $DirectoryHandle $0 "$INSTDIR\*.*"
  IfErrors done
loop:
  StrCmp $0 "" done
  StrCmp $0 "." next
  StrCmp $0 ".." next
  StrCpy $DirectoryEntry $0
  Goto done
next:
  ClearErrors
  FindNext $DirectoryHandle $0
  IfErrors done
  Goto loop
done:
  StrCmp $DirectoryHandle "" +2
    FindClose $DirectoryHandle
FunctionEnd

Function ValidateMarker
  ClearErrors
  FileOpen $0 "$INSTDIR\${MARKER_FILE}" r
  IfErrors invalid_marker
  FileRead $0 $MarkerContent
  ClearErrors
  FileRead $0 $MarkerExtra
  IfErrors +2 0
    StrCmp $MarkerExtra "" +1 invalid_marker_open
  FileClose $0
  StrCmp $MarkerContent "${MARKER_VALUE}" marker_ok
  Goto invalid_marker
invalid_marker_open:
  FileClose $0
invalid_marker:
  Push "The installation marker is missing or invalid."
  Call FailInstall
marker_ok:
FunctionEnd

Function un.ValidateMarker
  ClearErrors
  FileOpen $0 "$INSTDIR\${MARKER_FILE}" r
  IfErrors invalid_marker
  FileRead $0 $MarkerContent
  ClearErrors
  FileRead $0 $MarkerExtra
  IfErrors +2 0
    StrCmp $MarkerExtra "" +1 invalid_marker_open
  FileClose $0
  StrCmp $MarkerContent "${MARKER_VALUE}" marker_ok
  Goto invalid_marker
invalid_marker_open:
  FileClose $0
invalid_marker:
  Push "The installation marker is missing or invalid. No files were changed."
  Call un.Fail
marker_ok:
FunctionEnd

Function ReadInstallRegistry
  ReadRegStr $AppInstallLocation HKCU "${PRODUCT_KEY}" "InstallLocation"
  ReadRegStr $AppDisplayVersion HKCU "${PRODUCT_KEY}" "DisplayVersion"
  ReadRegStr $UninstallLocation HKCU "${UNINSTALL_KEY}" "InstallLocation"
  ReadRegStr $UninstallDisplayVersion HKCU "${UNINSTALL_KEY}" "DisplayVersion"
FunctionEnd

Function ValidateRegistry
  Call ReadInstallRegistry
  StrCmp $AppDisplayVersion "${PRODUCT_VERSION}" +2
    Goto invalid_registry
  StrCmp $UninstallDisplayVersion "${PRODUCT_VERSION}" +2
    Goto invalid_registry
  GetFullPathName $0 "$AppInstallLocation"
  StrCmp $0 $INSTDIR +2
    Goto invalid_registry
  GetFullPathName $0 "$UninstallLocation"
  StrCmp $0 $INSTDIR registry_ok
invalid_registry:
  Push "The existing installation registry values do not match this version and path."
  Call FailInstall
registry_ok:
FunctionEnd

Function ValidateRegistryAbsent
  StrCpy $FailureStage "validate_application_registry_absent"
  Push "${PRODUCT_KEY}"
  Call RegistryKeyExists64
  Pop $0
  StrCmp $0 "0" +2
    Goto stale_registry
  StrCpy $FailureStage "validate_uninstall_registry_absent"
  Push "${UNINSTALL_KEY}"
  Call RegistryKeyExists64
  Pop $0
  StrCmp $0 "0" registry_absent
stale_registry:
  Push "The target has an existing or inaccessible installation registry key."
  Call FailInstall
registry_absent:
FunctionEnd

Function RegistryKeyExists64
  Exch $0
  Push $1
  Push $2
  Push $3
  System::Call 'advapi32::RegOpenKeyExW(i 0x80000001, w r0, i 0, i 0x0101, *i .r1) i .r2'
  StrCmp $2 0 registry_key_exists
  StrCmp $2 2 registry_key_absent registry_key_error
registry_key_exists:
  System::Call 'advapi32::RegCloseKey(i r1)'
  StrCpy $0 "1"
  Goto registry_key_done
registry_key_absent:
  StrCpy $0 "0"
  Goto registry_key_done
registry_key_error:
  StrCpy $0 "error"
registry_key_done:
  Pop $3
  Pop $2
  Pop $1
  Exch $0
FunctionEnd

Function un.ValidateRegistry
  Call un.ReadInstallRegistry
  StrCmp $AppDisplayVersion "${PRODUCT_VERSION}" +2
    Goto invalid_registry
  StrCmp $UninstallDisplayVersion "${PRODUCT_VERSION}" +2
    Goto invalid_registry
  GetFullPathName $0 "$AppInstallLocation"
  StrCmp $0 $INSTDIR +2
    Goto invalid_registry
  GetFullPathName $0 "$UninstallLocation"
  StrCmp $0 $INSTDIR registry_ok
invalid_registry:
  Push "The installation registry values are invalid. No files were changed."
  Call un.Fail
registry_ok:
FunctionEnd

Function un.ReadInstallRegistry
  ReadRegStr $AppInstallLocation HKCU "${PRODUCT_KEY}" "InstallLocation"
  ReadRegStr $AppDisplayVersion HKCU "${PRODUCT_KEY}" "DisplayVersion"
  ReadRegStr $UninstallLocation HKCU "${UNINSTALL_KEY}" "InstallLocation"
  ReadRegStr $UninstallDisplayVersion HKCU "${UNINSTALL_KEY}" "DisplayVersion"
FunctionEnd

Function WriteInstallMarker
  ClearErrors
  FileOpen $0 "$INSTDIR\${MARKER_FILE}" w
  IfErrors marker_open_error
  ClearErrors
  FileWrite $0 "${MARKER_VALUE}"
  IfErrors marker_write_error
  FileClose $0
  Return
marker_write_error:
  FileClose $0
marker_open_error:
  Push "The installation marker could not be written."
  Call FailInstall
FunctionEnd

Function .onInit
  SetShellVarContext current
  SetRegView 64
!ifndef INIT_PROBE_RESULT
  Delete "$TEMP\${INSTALL_ERROR_LOG}"
!endif
  StrCpy $IsReinstall 0
  StrCpy $FailureStage "native_architecture"
  Call CheckNativeAmd64
  StrCpy $FailureStage "normalize_install_directory"
  Call NormalizeInstallDirectory
!ifdef INIT_PROBE_FORCE_FAILURE
  StrCpy $FailureStage "forced_init_probe_failure"
  Push "Forced initialization probe failure."
  Call FailInstall
!endif
  StrCpy $FailureStage "read_install_registry"
  Call ReadInstallRegistry

  IfFileExists "$INSTDIR\." directory_exists new_directory
new_directory:
  Call ValidateRegistryAbsent
  Goto first_install
directory_exists:
  StrCpy $FailureStage "inspect_install_directory"
  Call DirectoryHasEntries
  StrCmp $DirectoryEntry "" empty_directory nonempty_directory
empty_directory:
  Call ValidateRegistryAbsent
  Goto first_install
nonempty_directory:
  StrCpy $FailureStage "validate_install_marker"
  Call ValidateMarker
  StrCpy $FailureStage "validate_reinstall_registry"
  Call ValidateRegistry
  StrCpy $IsReinstall 1
first_install:
!ifdef INIT_PROBE_RESULT
!ifdef INIT_PROBE_WRITE_MARKER
  StrCpy $FailureStage "write_install_marker"
  SetErrors
  Call WriteInstallMarker
!endif
  ClearErrors
  FileOpen $0 "${INIT_PROBE_RESULT}" w
  IfErrors init_probe_write_error
  FileWriteUTF16LE /BOM $0 "stage=success$\r$\n"
  FileWriteUTF16LE $0 "native_architecture=$NativeArchitecture$\r$\n"
  FileWriteUTF16LE $0 "install_directory=$INSTDIR$\r$\n"
  FileWriteUTF16LE $0 "is_reinstall=$IsReinstall$\r$\n"
  FileClose $0
  SetErrorLevel 0
  Quit
init_probe_write_error:
  StrCpy $FailureStage "write_init_probe_result"
  Push "The initialization probe result could not be written."
  Call FailInstall
!endif
FunctionEnd

Section "Install" MainSection
  SetShellVarContext current
  SetRegView 64
  StrCmp $IsReinstall 1 0 +2
    RMDir /r "$INSTDIR\app"
  StrCmp $IsReinstall 1 0 +2
    IfFileExists "$INSTDIR\app\." reinstall_remove_error

  CreateDirectory "$INSTDIR\app"
  SetOutPath "$INSTDIR\app"
  File /r /x "${BUILD_OWNER_MARKER}" "${APP_SOURCE}\*.*"

  SetOutPath "$INSTDIR"
  StrCpy $FailureStage "write_install_marker"
  Call WriteInstallMarker
  WriteUninstaller "$INSTDIR\Uninstall.exe"

  CreateDirectory "$SMPROGRAMS\${START_MENU_DIR}"
  CreateShortcut "$SMPROGRAMS\${START_MENU_DIR}\${START_MENU_LINK}" "$INSTDIR\app\${PRODUCT_EXE}"

  WriteRegStr HKCU "${PRODUCT_KEY}" "InstallLocation" "$INSTDIR"
  WriteRegStr HKCU "${PRODUCT_KEY}" "DisplayVersion" "${PRODUCT_VERSION}"
  WriteRegStr HKCU "${UNINSTALL_KEY}" "DisplayName" "${PRODUCT_NAME}"
  WriteRegStr HKCU "${UNINSTALL_KEY}" "DisplayVersion" "${PRODUCT_VERSION}"
  WriteRegStr HKCU "${UNINSTALL_KEY}" "Publisher" "${PRODUCT_PUBLISHER}"
  WriteRegStr HKCU "${UNINSTALL_KEY}" "DisplayIcon" "$INSTDIR\app\${PRODUCT_EXE}"
  WriteRegStr HKCU "${UNINSTALL_KEY}" "InstallLocation" "$INSTDIR"
  WriteRegStr HKCU "${UNINSTALL_KEY}" "UninstallString" '$\"$INSTDIR\Uninstall.exe$\"'
  WriteRegStr HKCU "${UNINSTALL_KEY}" "QuietUninstallString" '$\"$INSTDIR\Uninstall.exe$\" /S'
  WriteRegDWORD HKCU "${UNINSTALL_KEY}" "NoModify" 1
  WriteRegDWORD HKCU "${UNINSTALL_KEY}" "NoRepair" 1
  ${GetSize} "$INSTDIR" "/S=0K" $0 $1 $2
  WriteRegDWORD HKCU "${UNINSTALL_KEY}" "EstimatedSize" $0
  Goto install_complete

reinstall_remove_error:
  Push "The previous application directory could not be removed."
  Call FailInstall
install_complete:
SectionEnd

Function un.onInit
  SetShellVarContext current
  SetRegView 64
  Call un.CheckNativeAmd64
  Call un.NormalizeInstallDirectory
  Call un.ValidateMarker
  Call un.ValidateRegistry
FunctionEnd

Section "Uninstall"
  SetShellVarContext current
  SetRegView 64
  ClearErrors
  RMDir /r "$INSTDIR\app"
  IfFileExists "$INSTDIR\app\." uninstall_app_error
  Delete "$SMPROGRAMS\${START_MENU_DIR}\${START_MENU_LINK}"
  IfFileExists "$SMPROGRAMS\${START_MENU_DIR}\${START_MENU_LINK}" uninstall_shortcut_error
  RMDir "$SMPROGRAMS\${START_MENU_DIR}"
  DeleteRegKey HKCU "${PRODUCT_KEY}"
  DeleteRegKey HKCU "${UNINSTALL_KEY}"
  StrCpy $0 ""
  ReadRegStr $0 HKCU "${PRODUCT_KEY}" "InstallLocation"
  StrCmp $0 "" +2
    Goto uninstall_registry_error
  StrCpy $0 ""
  ReadRegStr $0 HKCU "${UNINSTALL_KEY}" "InstallLocation"
  StrCmp $0 "" +2
    Goto uninstall_registry_error
  Delete "$INSTDIR\${MARKER_FILE}"
  IfFileExists "$INSTDIR\${MARKER_FILE}" uninstall_marker_error
  Delete "$INSTDIR\Uninstall.exe"
  RMDir "$INSTDIR"
  Goto uninstall_complete

uninstall_app_error:
  Push "The application directory could not be removed. Installation records were preserved."
  Call un.Fail
uninstall_marker_error:
  Push "The installation marker could not be removed. Installation records were preserved."
  Call un.Fail
uninstall_shortcut_error:
  Push "The Start menu shortcut could not be removed. Installation records were preserved."
  Call un.Fail
uninstall_registry_error:
  Push "The installation registry keys could not be removed. The marker was preserved."
  Call un.Fail
uninstall_complete:
SectionEnd
