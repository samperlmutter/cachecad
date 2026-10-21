param(
    [Parameter(Mandatory=$true)][string]$Executable,
    [string]$LaunchShortcut,
    [string]$SmokeReport,
    [switch]$AllowMissing
)
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$shell = New-Object -ComObject WScript.Shell
Add-Type @'
using System;
using System.Runtime.InteropServices;
using System.Runtime.InteropServices.ComTypes;
using System.Text;
public static class ShortcutPaths {
    [ComImport, Guid("000214F9-0000-0000-C000-000000000046"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    private interface IShellLinkW {
        void GetPath([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder path, int capacity, IntPtr data, uint flags);
    }
    public static string Read(string path, uint flags) {
        object link = Activator.CreateInstance(Type.GetTypeFromCLSID(new Guid("00021401-0000-0000-C000-000000000046")));
        try {
            ((IPersistFile)link).Load(path, 0);
            var buffer = new StringBuilder(32768);
            ((IShellLinkW)link).GetPath(buffer, buffer.Capacity, IntPtr.Zero, flags);
            return buffer.ToString();
        } finally { Marshal.ReleaseComObject(link); }
    }
}
'@
$paths = @{
    startMenu = Join-Path ([Environment]::GetFolderPath('Programs')) 'Permafrost.lnk'
    desktop = Join-Path ([Environment]::GetFolderPath('Desktop')) 'Permafrost.lnk'
}
$result = @{}
foreach ($name in $paths.Keys) {
    $path = $paths[$name]
    if (!(Test-Path -LiteralPath $path -PathType Leaf)) {
        if ($AllowMissing) {
            $result[$name] = @{ path = $path; exists = $false }
            continue
        }
        throw "Missing $name shortcut: $path"
    }
    $shortcut = $shell.CreateShortcut($path)
    if ([IO.Path]::GetFullPath($shortcut.TargetPath) -ine [IO.Path]::GetFullPath($Executable)) {
        throw "Wrong $name shortcut target: $($shortcut.TargetPath)"
    }
    if ($shortcut.Arguments -ne '') {
        throw "Unexpected $name shortcut arguments: $($shortcut.Arguments)"
    }
    $result[$name] = @{
        path = $path; exists = $true; target = $shortcut.TargetPath; arguments = $shortcut.Arguments
        rawTarget = [ShortcutPaths]::Read($path, 4)
        resolvedTarget = [ShortcutPaths]::Read($path, 0)
    }
}
if ($LaunchShortcut) {
    if (!$paths.ContainsKey($LaunchShortcut) -or !$SmokeReport) {
        throw 'LaunchShortcut must name an installed shortcut and requires SmokeReport'
    }
    # ShellExecute follows the actual installed .lnk, including its target and
    # AppUserModelID. No replacement shortcut is created for the smoke test.
    $process = Start-Process -FilePath $paths[$LaunchShortcut] -ArgumentList ('--smoke-output "{0}"' -f $SmokeReport) -PassThru
    if (!$process.WaitForExit(80000)) {
        $process.Kill()
        throw 'Shortcut-launched app did not exit within 80 seconds'
    }
    if ($process.ExitCode -ne 0) {
        throw "Shortcut-launched app exited with code $($process.ExitCode)"
    }
}
$result | ConvertTo-Json -Depth 4 -Compress
