# A time series of one process's audio level, to tell a looping bed from a
# notification chime. A steady level is something playing continuously; spikes
# with silence between them are alerts.
#
#   powershell -ExecutionPolicy Bypass -File tools/noise_series.ps1 -TargetPid 11412

param(
    [int]$TargetPid = 0,
    [int]$Seconds = 8
)

$ErrorActionPreference = "Stop"

Add-Type -Language CSharp @"
using System;
using System.Runtime.InteropServices;

public static class AudioSeries {
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

    public static float PeakFor(uint target) {
        var deviceEnum = (IMMDeviceEnumerator)(new MMDeviceEnumerator());
        IMMDevice device;
        deviceEnum.GetDefaultAudioEndpoint(0, 1, out device);
        object o;
        Guid mgrIid = typeof(IAudioSessionManager2).GUID;
        device.Activate(ref mgrIid, 1, IntPtr.Zero, out o);
        var mgr = (IAudioSessionManager2)o;
        IAudioSessionEnumerator sessions;
        mgr.GetSessionEnumerator(out sessions);
        int count; sessions.GetCount(out count);
        float best = 0;
        for (int i = 0; i < count; i++) {
            IAudioSessionControl2 ctl;
            sessions.GetSession(i, out ctl);
            uint pid; ctl.GetProcessId(out pid);
            if (pid != target) continue;
            float peak = 0;
            var meter = ctl as IAudioMeterInformation;
            if (meter != null) meter.GetPeakValue(out peak);
            if (peak > best) best = peak;
        }
        return best;
    }
}
"@

$name = (Get-Process -Id $TargetPid -ErrorAction SilentlyContinue).Name
Write-Output "sampling pid $TargetPid ($name) for ${Seconds}s"
Write-Output ""

$samples = @()
$end = (Get-Date).AddSeconds($Seconds)
while ((Get-Date) -lt $end) {
    $p = [AudioSeries]::PeakFor([uint32]$TargetPid)
    $samples += $p
    $bar = '#' * [int]($p * 200)
    Write-Output ("{0,7:N4} {1}" -f $p, $bar)
    Start-Sleep -Milliseconds 250
}

$loud = @($samples | Where-Object { $_ -gt 0.0005 })
Write-Output ""
Write-Output ("samples {0} · non-silent {1} · max {2:N4} · mean {3:N4}" -f `
    $samples.Count, $loud.Count,
    ($samples | Measure-Object -Maximum).Maximum,
    ($samples | Measure-Object -Average).Average)
if ($loud.Count -eq 0) {
    Write-Output "VERDICT: silent"
} elseif ($loud.Count -ge $samples.Count * 0.8) {
    Write-Output "VERDICT: playing continuously - something is looping"
} else {
    Write-Output "VERDICT: intermittent - alerts/notifications, not a bed"
}
