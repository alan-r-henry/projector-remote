// adb pass-through for scrcpy on the XElectron C9A projector.
// The projector's firmware deletes anything written to /data/local/tmp within a second,
// so scrcpy's helper never survives there. This forwards every call to the real adb and
// only rewrites that one path to /sdcard. Use with: set ADB=<this exe> before scrcpy,
// and REAL_ADB=<path to the real adb.exe> unless adb is on PATH.
using System;
using System.Diagnostics;
using System.Text;

static class AdbShim
{
    static readonly string RealAdb = Environment.GetEnvironmentVariable("REAL_ADB") ?? "adb";
    const string From = "/data/local/tmp/scrcpy-server.jar";
    const string To = "/sdcard/scrcpy-server.jar";

    static int Main(string[] args)
    {
        var line = new StringBuilder();
        foreach (var a in args)
        {
            if (line.Length > 0) line.Append(' ');
            line.Append(Quote(a.Replace(From, To)));
        }
        var psi = new ProcessStartInfo(RealAdb, line.ToString()) { UseShellExecute = false };
        using (var p = Process.Start(psi))
        {
            p.WaitForExit();
            return p.ExitCode;
        }
    }

    // Windows command-line quoting (CommandLineToArgvW rules).
    static string Quote(string s)
    {
        if (s.Length > 0 && s.IndexOfAny(new[] { ' ', '\t', '"' }) < 0) return s;
        var sb = new StringBuilder("\"");
        int slashes = 0;
        foreach (var c in s)
        {
            if (c == '\\') { slashes++; continue; }
            if (c == '"') sb.Append('\\', slashes * 2 + 1);
            else sb.Append('\\', slashes);
            slashes = 0;
            sb.Append(c);
        }
        sb.Append('\\', slashes * 2).Append('"');
        return sb.ToString();
    }
}
