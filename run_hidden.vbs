Set fso = CreateObject("Scripting.FileSystemObject")
scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)
target = scriptDir & "\" & WScript.Arguments(0)
CreateObject("WScript.Shell").Run "cmd /c """ & target & """", 0, False
