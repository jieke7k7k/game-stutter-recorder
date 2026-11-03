// LhmWrapper: v1.3 CPU-only temperature reader (LibreHardwareMonitor).
// Rules (user + Argon, do NOT relax):
// - IsCpuEnabled = true ONLY. Never enable Motherboard/Controller/Storage,
//   or the EC/SMBus polling itself creates micro-stutter.
// - No game injection, no settings changes. This process only reads sensors
//   and prints one JSON line per sample to stdout. Any driver failure exits
//   non-zero so the Python side degrades to PDH-only instead of crashing.
// - If the driver cannot load (anti-cheat, no admin), exit 2 with an error
//   line on stderr; the caller must NOT retry in a loop.
using System;
using System.Text.Json;
using LibreHardwareMonitor.Hardware;

class Program
{
    static int Main(string[] args)
    {
        double intervalMs = 250;
        if (args.Length > 0) double.TryParse(args[0], out intervalMs);
        if (intervalMs < 100) intervalMs = 100;
        if (intervalMs > 2000) intervalMs = 2000;
        try
        {
            var computer = new Computer { IsCpuEnabled = true };
            computer.Open();
            // Marker for uninstall: the wrapper ran, so the driver may exist.
            // The Python side writes lhm_driver_installed.txt next to the exe
            // only after this process exits 0 at least once; this process
            // itself never writes outside stdout/stderr.
            while (true)
            {
                string line = ReadCpu(computer);
                if (line == null)
                {
                    Console.Error.WriteLine("no cpu sensor");
                    return 3;
                }
                Console.WriteLine(line);
                Console.Out.Flush();
                System.Threading.Thread.Sleep((int)intervalMs);
                // Loop until the parent kills this process on stop.
                // No stdin Peek here: Peek on a console blocks and would
                // hang the sampler.
            }
            computer.Close();
            return 0;
        }
        catch (Exception ex)
        {
            Console.Error.WriteLine("lhm failed: " + ex.Message);
            return 2;
        }
    }

    static string ReadCpu(Computer computer)
    {
        foreach (var hw in computer.Hardware)
        {
            if (hw.HardwareType != HardwareType.Cpu) continue;
            hw.Update();
            double? temp = null, power = null, clock = null;
            foreach (var s in hw.Sensors)
            {
                if (s.SensorType == SensorType.Temperature && s.Name.Contains("Core"))
                {
                    if (s.Value.HasValue) temp = temp == null ? s.Value.Value : Math.Max(temp.Value, s.Value.Value);
                }
                if (s.SensorType == SensorType.Power && s.Name.Contains("Package"))
                {
                    if (s.Value.HasValue) power = s.Value.Value;
                }
                if (s.SensorType == SensorType.Clock && s.Name.Contains("Core"))
                {
                    if (s.Value.HasValue) clock = clock == null ? s.Value.Value : Math.Max(clock.Value, s.Value.Value);
                }
            }
            // Package temperature is the fallback when no Core sensor exists.
            if (temp == null)
            {
                foreach (var s in hw.Sensors)
                {
                    if (s.SensorType == SensorType.Temperature && s.Name.Contains("Package"))
                    {
                        if (s.Value.HasValue) temp = s.Value.Value;
                    }
                }
            }
            var obj = new { temp_c = temp, power_w = power, clock_mhz = clock };
            return JsonSerializer.Serialize(obj);
        }
        return null;
    }
}
