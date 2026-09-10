Set objFSO   = CreateObject("Scripting.FileSystemObject")
Set objShell = CreateObject("WScript.Shell")
Set objWMI   = GetObject("winmgmts:\\.\root\cimv2")

' Single-instance guard: check for an existing catxt_app.py process
Set colProcs = objWMI.ExecQuery("SELECT * FROM Win32_Process WHERE Name='pythonw.exe'")
For Each oProc In colProcs
    If InStr(LCase(oProc.CommandLine), "catxt_app.py") > 0 Then
        MsgBox "CATXT Sync is already running." & Chr(13) & Chr(10) & Chr(13) & Chr(10) & _
               "Check the system tray.", 48, "CATXT Sync"
        WScript.Quit
    End If
Next

strDir    = objFSO.GetParentFolderName(WScript.ScriptFullName)
strScript = strDir & "\catxt_app.py"

' Run pythonw so no console window appears (same behaviour as the compiled exe)
objShell.Run "pythonw.exe """ & strScript & """", 0, False
