Set fso = CreateObject("Scripting.FileSystemObject")
base = fso.GetParentFolderName(WScript.ScriptFullName)
Set ws = CreateObject("WScript.Shell")
ws.CurrentDirectory = base
if fso.FileExists(base & "\runtime\pythonw.exe") then
  py = base & "\runtime\pythonw.exe"
elseif fso.FileExists(base & "\.venv\Scripts\pythonw.exe") then
  py = base & "\.venv\Scripts\pythonw.exe"
else
  py = "pythonw.exe"
end if
On Error Resume Next
ws.Run """" & py & """ """ & base & "\hainnu_proxy.py""", 0, False
if Err.Number <> 0 then
  Set lf = fso.OpenTextFile(base & "\hainnu_proxy.out.log", 8, True)
  lf.WriteLine "[" & Now & "] [autostart] start failed: 0x" & Hex(Err.Number) & " " & Err.Description
  lf.Close
end if
