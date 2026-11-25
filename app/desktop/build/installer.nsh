; NSIS additions for the CreatorOS installer.
;
; Two things the generated script cannot know:
;
;  1. The shortcut icon. CreatorOS.exe keeps the vendor's Electron signature (see
;     docs/ADR/ADR-34-Release-Packaging.md), which means its *own* embedded icon is Electron's, and
;     electron-builder points the .lnk files at "$INSTDIR\CreatorOS.exe,0". We ship the real icon as a
;     resource and recreate both shortcuts with it, so Explorer, the Start Menu and the taskbar all
;     show the CreatorOS mark.
;
;  2. A note in the install log about where user data lives, because the installer never touches it.
;
; electron-builder runs the customInstall macro after it has created $INSTDIR and its own shortcuts.

!macro customInstall
  ${If} ${FileExists} "$INSTDIR\resources\icon.ico"
    CreateShortCut "$DESKTOP\${SHORTCUT_NAME}.lnk" "$INSTDIR\${APP_EXECUTABLE_FILENAME}" "" "$INSTDIR\resources\icon.ico" 0 SW_SHOWNORMAL "" "${APP_DESCRIPTION}"
    CreateShortCut "$SMPROGRAMS\${SHORTCUT_NAME}.lnk" "$INSTDIR\${APP_EXECUTABLE_FILENAME}" "" "$INSTDIR\resources\icon.ico" 0 SW_SHOWNORMAL "" "${APP_DESCRIPTION}"
    DetailPrint "Shortcuts recreated with the CreatorOS icon."
  ${EndIf}
  DetailPrint "CreatorOS keeps its data outside this folder (default %USERPROFILE%\CreatorOS); uninstalling does not delete it."
!macroend

!macro customUnInstall
  ; The uninstaller removes the shortcuts it created; if the icon override created them again under a
  ; different name, remove that too. User data is deliberately left alone.
  ${If} ${FileExists} "$DESKTOP\${SHORTCUT_NAME}.lnk"
    Delete "$DESKTOP\${SHORTCUT_NAME}.lnk"
  ${EndIf}
  ${If} ${FileExists} "$SMPROGRAMS\${SHORTCUT_NAME}.lnk"
    Delete "$SMPROGRAMS\${SHORTCUT_NAME}.lnk"
  ${EndIf}
!macroend
