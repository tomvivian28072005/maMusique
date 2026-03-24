; Override le check "app is running" par défaut de electron-builder
; Au lieu de demander à l'utilisateur de fermer manuellement, on tue les processus
!macro customCheckAppRunning
  ; Tuer Clom (Electron) et le serveur Python s'ils tournent
  nsExec::ExecToStack 'taskkill /F /IM "Clom.exe" /T'
  Sleep 1000
!macroend
