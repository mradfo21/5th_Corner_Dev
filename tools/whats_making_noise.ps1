# Which processes are actually playing audio right now.
#
# Written because "I hear sound coming from somewhere" is otherwise answered by
# guessing, and the wrong guess means killing an app someone was using. This
# asks Windows directly: it walks the audio sessions on the default playback
# device and reads each one's peak meter, so a process that merely *could* make
# noise is distinguishable from one that is.
#
#   powershell -ExecutionPolicy Bypass -File tools/whats_making_noise.ps1

$ErrorActionPreference = "Stop"

Add-Type -Language CSharp @"
using System;
using System.Runtime.InteropServices;

public static class Audio {
    [ComImport, Guid("BCDE0395-E52F-467C-8E3D-C4579291692E")]
    class MMDeviceEnumerator { }

    [Guid("A95664D2-9614-4F35-A746-DE8DB63617E6"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    interface IMMDeviceEnumerator {
        int NotImpl1();
        int GetDefaultAudioEndpoint(int dataFlow, int role, out IMMDevice ppDevice);
    }

    [Guid("D666063F-1587-4E43-81F1-B948E807363F"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    interface IMMDevice {
        int Activate(ref Guid iid, int dwClsCtx, IntPtr pActivationParams,
                     [MarshalAs(UnmanagedType.IUnknown)] out object ppInterface);
    }

    [Guid("77AA99A0-1BD6-484F-8BC7-2C654C9A9B6F"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    interface IAudioSessionManager2 {
        int NotImpl1(); int NotImpl2();
        int GetSessionEnumerator(out IAudioSessionEnumerator SessionEnum);
    }

    [Guid("E2F5BB11-0570-40CA-ACDD-3AA01277DEE8"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    interface IAudioSessionEnumerator {
        int GetCount(out int SessionCount);
        int GetSession(int SessionCount, out IAudioSessionControl2 Session);
    }

    [Guid("BFB7FF88-7239-4FC9-8FA2-07C950BE9C6D"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    interface IAudioSessionControl2 {
        int NotImpl0();
        int GetDisplayName(out IntPtr name);
        int SetDisplayName(string value, ref Guid ev);
        int GetIconPath(out IntPtr path);
        int SetIconPath(string value, ref Guid ev);
        int GetGroupingParam(out Guid group);
        int SetGroupingParam(Guid group, ref Guid ev);
        int NotImpl1(); int NotImpl2();
        int GetSessionIdentifier(out IntPtr id);
        int GetSessionInstanceIdentifier(out IntPtr id);
        int GetProcessId(out uint pid);
    }

    [Guid("C02216F6-8C67-4B5B-9D00-D008E73E0064"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    interface IAudioMeterInformation {
        int GetPeakValue(out float peak);
    }

    public struct Session { public uint Pid; public float Peak; public int State; }

    public static Session[] Active() {
        var deviceEnum = (IMMDeviceEnumerator)(new MMDeviceEnumerator());
        IMMDevice device;
        deviceEnum.GetDefaultAudioEndpoint(0 /* render */, 1 /* multimedia */, out device);

        object o;
        Guid mgrIid = typeof(IAudioSessionManager2).GUID;
        device.Activate(ref mgrIid, 1, IntPtr.Zero, out o);
        var mgr = (IAudioSessionManager2)o;

        IAudioSessionEnumerator sessions;
        mgr.GetSessionEnumerator(out sessions);
        int count; sessions.GetCount(out count);

        var found = new System.Collections.Generic.List<Session>();
        for (int i = 0; i < count; i++) {
            IAudioSessionControl2 ctl;
            sessions.GetSession(i, out ctl);
            uint pid; ctl.GetProcessId(out pid);
            float peak = 0;
            var meter = ctl as IAudioMeterInformation;
            if (meter != null) meter.GetPeakValue(out peak);
            found.Add(new Session { Pid = pid, Peak = peak, State = 0 });
        }
        return found.ToArray();
    }
}
"@

# One reading catches a silent moment in a loud track, so sample for a second
# and keep the loudest value seen per process.
$peak = @{}
for ($i = 0; $i -lt 10; $i++) {
    foreach ($s in [Audio]::Active()) {
        if ($s.Pid -eq 0) { continue }
        if (-not $peak.ContainsKey($s.Pid) -or $peak[$s.Pid] -lt $s.Peak) {
            $peak[$s.Pid] = $s.Peak
        }
    }
    Start-Sleep -Milliseconds 100
}

$rows = foreach ($k in $peak.Keys) {
    $p = Get-Process -Id $k -ErrorAction SilentlyContinue
    [PSCustomObject]@{
        Pid     = $k
        Name    = if ($p) { $p.Name } else { "(gone)" }
        Peak    = [math]::Round($peak[$k], 4)
        Audible = if ($peak[$k] -gt 0.0001) { "YES" } else { "" }
    }
}

$rows | Sort-Object Peak -Descending | Format-Table -AutoSize
