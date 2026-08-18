
using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.Drawing.Imaging;
using System.Globalization;
using System.IO;
using System.IO.Pipes;
using System.Runtime.InteropServices;
using System.Text;
using System.Windows.Automation;

// Ava Windows permissions-helper — the user-session desktop-automation daemon.
//
// Holds no TCC-like grant: on Windows the "permission" IS the session identity,
// so this helper must run in the user's interactive session (Session 1 /
// WinSta0). The converge phase builds it with the .NET Framework csc.exe every
// Windows install ships (services/permissions_helper/windows/lifecycle.py) and
// registers it as the logon scheduled task "AvaPermissionsHelper" (/IT), which
// is what lands it next to the user.
//
// Named pipe JSON-line server (\\.\pipe\ava-permissions-helper); protocol
// aligned with the macOS permissions-helper (services/permissions_helper/
// helper/main.swift): {"id":N,"method":"...","args"} -> {"id":N,"ok":true,...}.
// Methods: ping, screencapture_region, type, click, key, window_info,
// session_info, close_popups. Startup self-check reports session facts in ping
// (user_interactive / window_station / foreground_pid) so a helper that landed
// in Session 0 says so instead of silently failing.
public static class Program
{
    [StructLayout(LayoutKind.Sequential)]
    public struct INPUT { public uint type; public InputUnion U; }

    [StructLayout(LayoutKind.Explicit)]
    public struct InputUnion
    {
        [FieldOffset(0)] public MOUSEINPUT mi;
        [FieldOffset(0)] public KEYBDINPUT ki;
    }

    [StructLayout(LayoutKind.Sequential)]
    public struct KEYBDINPUT { public ushort wVk; public ushort wScan; public uint dwFlags; public uint time; public IntPtr dwExtraInfo; }

    [StructLayout(LayoutKind.Sequential)]
    public struct MOUSEINPUT { public int dx; public int dy; public uint mouseData; public uint dwFlags; public uint time; public IntPtr dwExtraInfo; }

    [StructLayout(LayoutKind.Sequential)]
    public struct RECT { public int Left; public int Top; public int Right; public int Bottom; }

    [DllImport("user32.dll", SetLastError = true)]
    static extern uint SendInput(uint nInputs, INPUT[] pInputs, int cbSize);

    [DllImport("user32.dll")]
    static extern bool SetProcessDPIAware();

    [DllImport("user32.dll")]
    static extern IntPtr GetProcessWindowStation();

    [DllImport("user32.dll")]
    static extern IntPtr GetForegroundWindow();

    [DllImport("user32.dll", CharSet = CharSet.Unicode)]
    static extern int GetWindowText(IntPtr hWnd, StringBuilder lpString, int nMaxCount);

    [DllImport("user32.dll")]
    static extern bool GetWindowRect(IntPtr hWnd, out RECT lpRect);

    [DllImport("user32.dll")]
    static extern uint GetWindowThreadProcessId(IntPtr hWnd, out uint lpdwProcessId);

    [DllImport("user32.dll")]
    static extern int GetSystemMetrics(int nIndex);

    [DllImport("user32.dll", CharSet = CharSet.Unicode)]
    static extern bool GetUserObjectInformation(IntPtr hObj, int nIndex, IntPtr pvInfo, uint nLength, out uint lpnLengthNeeded);

    [DllImport("wtsapi32.dll")]
    static extern bool WTSQuerySessionInformation(IntPtr hServer, uint SessionId, int WTSInfoClass, out IntPtr ppBuffer, out uint pBytesReturned);

    [DllImport("wtsapi32.dll")]
    static extern void WTSFreeMemory(IntPtr pMemory);

    const uint KEYEVENTF_KEYUP = 0x0002;
    const uint KEYEVENTF_UNICODE = 0x0004;
    const uint MOUSEEVENTF_ABSOLUTE = 0x8000;
    const uint MOUSEEVENTF_MOVE = 0x0001;
    const uint MOUSEEVENTF_LEFTDOWN = 0x0002;
    const uint MOUSEEVENTF_LEFTUP = 0x0004;
    const ushort VK_CONTROL = 0x11;

    static string LogPath = "";

    static void Main(string[] args)
    {
        // DPI-aware from the start: at 200% scaling the system metrics and
        // window rects come back in physical pixels, which is what the client's
        // coordinates are in. DPI-unaware, GetSystemMetrics/GetWindowRect mix
        // logical and physical units and clicks land in the wrong place.
        SetProcessDPIAware();
        string dir = AppDomain.CurrentDomain.BaseDirectory;
        LogPath = Path.Combine(dir, "ava-permissions-helper.log");
        Log("=== helper starting pid=" + Process.GetCurrentProcess().Id +
            " session=" + Process.GetCurrentProcess().SessionId +
            " userInteractive=" + Environment.UserInteractive +
            " winSta=" + GetWinStationName() +
            " fg=" + (GetForegroundWindow() != IntPtr.Zero));
        if (!Environment.UserInteractive || GetWinStationName() != "WinSta0")
            Log("WARNING: not in interactive user session; desktop automation will fail");

        while (true)
        {
            try
            {
                using (NamedPipeServerStream pipe = new NamedPipeServerStream("ava-permissions-helper", PipeDirection.InOut, 1, PipeTransmissionMode.Byte, PipeOptions.None))
                {
                    pipe.WaitForConnection();
                    Log("client connected");
                    using (StreamReader sr = new StreamReader(pipe, new UTF8Encoding(false)))
                    using (StreamWriter sw = new StreamWriter(pipe, new UTF8Encoding(false)))
                    {
                        string line;
                        while ((line = sr.ReadLine()) != null)
                        {
                            if (line.Trim().Length == 0) continue;
                            string resp = ProcessLine(line);
                            sw.WriteLine(resp);
                            sw.Flush();
                        }
                    }
                    pipe.Disconnect();
                    Log("client disconnected");
                }
            }
            catch (Exception ex)
            {
                Log("pipe error: " + ex.Message);
                System.Threading.Thread.Sleep(500);
            }
        }
    }

    static string ProcessLine(string line)
    {
        long id = 0;
        string method = "";
        try
        {
            object parsed = ParseJson(line);
            Dictionary<string, object> req = parsed as Dictionary<string, object>;
            if (req == null) throw new Exception("request must be a JSON object");
            id = GetLong(req, "id", 0);
            method = GetString(req, "method", "");
            if (method.Length == 0) throw new Exception("method required");
            object result = Dispatch(method, req);
            Log("OK " + method + " id=" + id);
            return "{\"id\":" + id.ToString(CultureInfo.InvariantCulture) + ",\"ok\":true,\"result\":" + JsonSerialize(result) + "}";
        }
        catch (Exception ex)
        {
            Log("ERR " + method + " id=" + id + ": " + ex.Message);
            return "{\"id\":" + id.ToString(CultureInfo.InvariantCulture) + ",\"ok\":false,\"error\":" + JsonSerialize(ex.Message) + "}";
        }
    }

    static object Dispatch(string method, Dictionary<string, object> req)
    {
        switch (method)
        {
            case "ping":
                Dictionary<string, object> p = new Dictionary<string, object>();
                p["pong"] = true;
                p["user_interactive"] = Environment.UserInteractive;
                p["window_station"] = GetWinStationName();
                p["session_id"] = (long)Process.GetCurrentProcess().SessionId;
                p["foreground_pid"] = (long)FgPid();
                // Screen Recording grant analog: the helper is usable when it
                // runs in the user's interactive session on the real window
                // station. Client's check_screen_capture keys off this field.
                p["preflight_screen"] = Environment.UserInteractive
                    && GetWinStationName() == "WinSta0";
                return p;
            case "session_info":
                return SessionInfo();
            case "window_info":
                return WindowInfo(GetString(req, "owner", null));
            case "screencapture_region":
                return ScreenCapture(GetInt(req, "x"), GetInt(req, "y"), GetInt(req, "w"), GetInt(req, "h"), GetString(req, "path", ""));
            case "type":
                return TypeText(GetString(req, "text", ""), GetString(req, "mode", "unicode"));
            case "click":
                return Click(GetInt(req, "x"), GetInt(req, "y"));
            case "key":
                return Key(GetInt(req, "code"), GetBool(req, "cmd"));
            case "close_popups":
                return ClosePopups();
            default:
                throw new Exception("unknown method: " + method);
        }
    }

    static Dictionary<string, object> SessionInfo()
    {
        int sid = Process.GetCurrentProcess().SessionId;
        uint state = uint.MaxValue;
        try
        {
            IntPtr buf;
            uint bytes;
            if (WTSQuerySessionInformation(IntPtr.Zero, (uint)sid, 8, out buf, out bytes))
            {
                state = (uint)Marshal.ReadInt32(buf);
                WTSFreeMemory(buf);
            }
        }
        catch { }
        Dictionary<string, object> r = new Dictionary<string, object>();
        r["locked"] = (state != 0);       // WTSActive == 0
        r["on_console"] = (sid != 0);
        r["connect_state"] = (long)state;
        return r;
    }

    static Dictionary<string, object> WindowInfo(string owner)
    {
        IntPtr h = IntPtr.Zero;
        string procName = "";
        if (string.IsNullOrEmpty(owner))
        {
            h = GetForegroundWindow();
            if (h == IntPtr.Zero) throw new Exception("no foreground window");
            uint pid;
            GetWindowThreadProcessId(h, out pid);
            try { procName = Process.GetProcessById((int)pid).ProcessName; }
            catch { procName = "pid" + pid.ToString(); }
        }
        else
        {
            Process[] ps = Process.GetProcessesByName(owner);
            if (ps.Length == 0) throw new Exception("no process named " + owner);
            foreach (Process p in ps)
            {
                if (p.MainWindowHandle != IntPtr.Zero) { h = p.MainWindowHandle; procName = p.ProcessName; break; }
            }
            if (h == IntPtr.Zero) throw new Exception("no main window for " + owner);
        }
        RECT r;
        if (!GetWindowRect(h, out r)) throw new Exception("GetWindowRect failed");
        StringBuilder sb = new StringBuilder(512);
        GetWindowText(h, sb, sb.Capacity);
        Dictionary<string, object> res = new Dictionary<string, object>();
        res["x"] = (long)r.Left;
        res["y"] = (long)r.Top;
        res["w"] = (long)(r.Right - r.Left);
        res["h"] = (long)(r.Bottom - r.Top);
        res["owner"] = procName;
        res["title"] = sb.ToString();
        return res;
    }

    static Dictionary<string, object> ScreenCapture(int x, int y, int w, int h, string path)
    {
        if (string.IsNullOrEmpty(path)) throw new Exception("path required");
        if (w <= 0 || h <= 0) throw new Exception("invalid region");
        using (Bitmap bmp = new Bitmap(w, h))
        {
            using (Graphics g = Graphics.FromImage(bmp))
            {
                g.CopyFromScreen(x, y, 0, 0, new Size(w, h));
            }
            bmp.Save(path, ImageFormat.Png);
        }
        Dictionary<string, object> r = new Dictionary<string, object>();
        r["path"] = path;
        r["bytes"] = new FileInfo(path).Length;
        return r;
    }

    static Dictionary<string, object> TypeText(string text, string mode)
    {
        FocusEditorIfPossible(GetForegroundWindow());
        Dictionary<string, object> r = new Dictionary<string, object>();
        if (mode == "clipboard")
        {
            return TypeViaClipboard(text, r, "explicit");
        }
        // non-ASCII text always goes through the clipboard path (more reliable for IME/UTF-16)
        foreach (char ch in text)
        {
            if (ch > 0x7F)
            {
                return TypeViaClipboard(text, r, "auto-nonascii");
            }
        }
        // unicode mode: send per-character (down+up) with inter-character delay;
        // on send failure retry once, then fall back to clipboard for the remaining text
        int size = Marshal.SizeOf(typeof(INPUT));
        uint typed = 0;
        for (int idx = 0; idx < text.Length; idx++)
        {
            char ch = text[idx];
            INPUT[] pair = new INPUT[2];
            pair[0] = MakeKey(0, (ushort)ch, KEYEVENTF_UNICODE);
            pair[1] = MakeKey(0, (ushort)ch, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP);
            uint n = SendInput(2, pair, size);
            if (n == 2)
            {
                typed++;
            }
            else
            {
                // retry once
                System.Threading.Thread.Sleep(20);
                n = SendInput(2, pair, size);
                if (n == 2)
                {
                    typed++;
                }
                else
                {
                    // clipboard fallback for this and the remaining characters
                    string rest = text.Substring(idx);
                    return TypeViaClipboard(rest, r, "fallback@" + idx);
                }
            }
            System.Threading.Thread.Sleep(10);
        }
        r["typed"] = (long)typed;
        r["mode"] = "unicode";
        return r;
    }

    static Dictionary<string, object> TypeViaClipboard(string text, Dictionary<string, object> r, string why)
    {
        if (!SetClipboardText(text)) throw new Exception("clipboard open failed: " + why);
        List<INPUT> l = new List<INPUT>();
        l.Add(MakeKey(VK_CONTROL, 0, 0));
        l.Add(MakeKey(0x56, 0, 0));
        l.Add(MakeKey(0x56, 0, KEYEVENTF_KEYUP));
        l.Add(MakeKey(VK_CONTROL, 0, KEYEVENTF_KEYUP));
        uint n = SendInput((uint)l.Count, l.ToArray(), Marshal.SizeOf(typeof(INPUT)));
        r["typed"] = (long)text.Length;
        r["mode"] = "clipboard";
        r["fallback"] = why;
        r["injected"] = (long)n;
        return r;
    }

    static bool SetClipboardText(string text)
    {
        if (!OpenClipboard(IntPtr.Zero)) return false;
        try
        {
            EmptyClipboard();
            byte[] bytes = Encoding.Unicode.GetBytes(text + "\0");
            IntPtr h = GlobalAlloc(0x0042 /*GMEM_MOVEABLE|GMEM_ZEROINIT*/, (UIntPtr)bytes.Length);
            if (h == IntPtr.Zero) return false;
            IntPtr p = GlobalLock(h);
            if (p == IntPtr.Zero) { GlobalFree(h); return false; }
            Marshal.Copy(bytes, 0, p, bytes.Length);
            GlobalUnlock(h);
            if (SetClipboardData(13 /*CF_UNICODETEXT*/, h) == IntPtr.Zero) { GlobalFree(h); return false; }
            return true;
        }
        finally
        {
            CloseClipboard();
        }
    }

    [DllImport("user32.dll")]
    static extern bool OpenClipboard(IntPtr hWndNewOwner);

    [DllImport("user32.dll")]
    static extern bool EmptyClipboard();

    [DllImport("user32.dll")]
    static extern IntPtr SetClipboardData(uint uFormat, IntPtr hMem);

    [DllImport("user32.dll")]
    static extern bool CloseClipboard();

    [DllImport("kernel32.dll")]
    static extern IntPtr GlobalAlloc(uint uFlags, UIntPtr dwBytes);

    [DllImport("kernel32.dll")]
    static extern IntPtr GlobalLock(IntPtr hMem);

    [DllImport("kernel32.dll")]
    static extern bool GlobalUnlock(IntPtr hMem);

    [DllImport("kernel32.dll")]
    static extern IntPtr GlobalFree(IntPtr hMem);

    static Dictionary<string, object> Click(int x, int y)
    {
        int sw = GetSystemMetrics(0);
        int sh = GetSystemMetrics(1);
        int cx = x * 65535 / sw;
        int cy = y * 65535 / sh;
        INPUT down = MakeMouse(cx, cy, MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_MOVE | MOUSEEVENTF_LEFTDOWN);
        INPUT up = MakeMouse(cx, cy, MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_MOVE | MOUSEEVENTF_LEFTUP);
        uint n1 = SendInput(1, new INPUT[] { down }, Marshal.SizeOf(typeof(INPUT)));
        uint n2 = SendInput(1, new INPUT[] { up }, Marshal.SizeOf(typeof(INPUT)));
        Dictionary<string, object> r = new Dictionary<string, object>();
        Dictionary<string, object> pt = new Dictionary<string, object>();
        pt["x"] = (long)x;
        pt["y"] = (long)y;
        r["clicked"] = pt;
        r["injected"] = (long)(n1 + n2);
        return r;
    }

    static Dictionary<string, object> Key(int code, bool ctrl)
    {
        List<INPUT> list = new List<INPUT>();
        if (ctrl) list.Add(MakeKey(VK_CONTROL, 0, 0));
        list.Add(MakeKey((ushort)code, 0, 0));
        list.Add(MakeKey((ushort)code, 0, KEYEVENTF_KEYUP));
        if (ctrl) list.Add(MakeKey(VK_CONTROL, 0, KEYEVENTF_KEYUP));
        uint n = SendInput((uint)list.Count, list.ToArray(), Marshal.SizeOf(typeof(INPUT)));
        Dictionary<string, object> r = new Dictionary<string, object>();
        r["pressed"] = (long)n;
        return r;
    }

    static Dictionary<string, object> ClosePopups()
    {
        int closed = 0;
        try
        {
            IntPtr fg = GetForegroundWindow();
            if (fg == IntPtr.Zero) throw new Exception("no foreground window");
            AutomationElement el = AutomationElement.FromHandle(fg);
            AutomationElementCollection wins = el.FindAll(TreeScope.Descendants,
                new PropertyCondition(AutomationElement.ControlTypeProperty, ControlType.Window));
            foreach (AutomationElement w in wins)
            {
                string cls = w.Current.ClassName;
                if (cls == null) continue;
                if (cls.IndexOf("Popup") < 0 && cls.IndexOf("TeachingTip") < 0) continue;
                AutomationElementCollection btns = w.FindAll(TreeScope.Descendants,
                    new PropertyCondition(AutomationElement.ControlTypeProperty, ControlType.Button));
                foreach (AutomationElement b in btns)
                {
                    string n = b.Current.Name;
                    if (n == null) continue;
                    if (n.IndexOf("OK") >= 0 || n.IndexOf("Close") >= 0 ||
                        n.IndexOf("Restart") >= 0 || n.IndexOf("关闭") >= 0 || n.IndexOf("确定") >= 0)
                    {
                        try
                        {
                            object pat;
                            if (b.TryGetCurrentPattern(InvokePattern.Pattern, out pat))
                            {
                                ((InvokePattern)pat).Invoke();
                                closed++;
                            }
                        }
                        catch { }
                    }
                }
            }
        }
        catch { }
        Dictionary<string, object> r = new Dictionary<string, object>();
        r["closed"] = (long)closed;
        return r;
    }

    static void FocusEditorIfPossible(IntPtr fg)
    {
        try
        {
            if (fg == IntPtr.Zero) return;
            AutomationElement el = AutomationElement.FromHandle(fg);
            AutomationElement edit = el.FindFirst(TreeScope.Descendants,
                new PropertyCondition(AutomationElement.ControlTypeProperty, ControlType.Document));
            if (edit == null)
                edit = el.FindFirst(TreeScope.Descendants,
                    new PropertyCondition(AutomationElement.ControlTypeProperty, ControlType.Edit));
            if (edit != null) edit.SetFocus();
        }
        catch { }
    }

    static INPUT MakeKey(ushort vk, ushort scan, uint flags)
    {
        INPUT i = new INPUT();
        i.type = 1;
        i.U.ki.wVk = vk;
        i.U.ki.wScan = scan;
        i.U.ki.dwFlags = flags;
        i.U.ki.time = 0;
        i.U.ki.dwExtraInfo = IntPtr.Zero;
        return i;
    }

    static INPUT MakeMouse(int dx, int dy, uint flags)
    {
        INPUT i = new INPUT();
        i.type = 0;
        i.U.mi.dx = dx;
        i.U.mi.dy = dy;
        i.U.mi.mouseData = 0;
        i.U.mi.dwFlags = flags;
        i.U.mi.time = 0;
        i.U.mi.dwExtraInfo = IntPtr.Zero;
        return i;
    }

    static string GetWinStationName()
    {
        IntPtr h = GetProcessWindowStation();
        uint needed = 0;
        GetUserObjectInformation(h, 2, IntPtr.Zero, 0, out needed);
        IntPtr buf = Marshal.AllocHGlobal((int)needed);
        try
        {
            GetUserObjectInformation(h, 2, buf, needed, out needed);
            return Marshal.PtrToStringUni(buf);
        }
        finally { Marshal.FreeHGlobal(buf); }
    }

    static uint FgPid()
    {
        IntPtr h = GetForegroundWindow();
        uint pid = 0;
        GetWindowThreadProcessId(h, out pid);
        return pid;
    }

    // ---------- minimal JSON ----------
    static object ParseJson(string s)
    {
        int i = 0;
        object v = ParseValue(s, ref i);
        return v;
    }

    static void SkipWs(string s, ref int i)
    {
        while (i < s.Length && (s[i] == ' ' || s[i] == '\t' || s[i] == '\r' || s[i] == '\n')) i++;
    }

    static object ParseValue(string s, ref int i)
    {
        SkipWs(s, ref i);
        if (i >= s.Length) throw new Exception("json: unexpected eof");
        char c = s[i];
        if (c == '{') return ParseObject(s, ref i);
        if (c == '[') return ParseArray(s, ref i);
        if (c == '"') return ParseString(s, ref i);
        if (c == 't') { i += 4; return true; }
        if (c == 'f') { i += 5; return false; }
        if (c == 'n') { i += 4; return null; }
        return ParseNumber(s, ref i);
    }

    static Dictionary<string, object> ParseObject(string s, ref int i)
    {
        Dictionary<string, object> d = new Dictionary<string, object>();
        i++;
        SkipWs(s, ref i);
        if (i < s.Length && s[i] == '}') { i++; return d; }
        while (true)
        {
            SkipWs(s, ref i);
            string key = ParseString(s, ref i);
            SkipWs(s, ref i);
            if (s[i] != ':') throw new Exception("json: expect ':'");
            i++;
            object v = ParseValue(s, ref i);
            d[key] = v;
            SkipWs(s, ref i);
            if (s[i] == ',') { i++; continue; }
            if (s[i] == '}') { i++; return d; }
            throw new Exception("json: expect ',' or '}'");
        }
    }

    static List<object> ParseArray(string s, ref int i)
    {
        List<object> l = new List<object>();
        i++;
        SkipWs(s, ref i);
        if (i < s.Length && s[i] == ']') { i++; return l; }
        while (true)
        {
            l.Add(ParseValue(s, ref i));
            SkipWs(s, ref i);
            if (s[i] == ',') { i++; continue; }
            if (s[i] == ']') { i++; return l; }
            throw new Exception("json: expect ',' or ']'");
        }
    }

    static string ParseString(string s, ref int i)
    {
        i++;
        StringBuilder sb = new StringBuilder();
        while (i < s.Length)
        {
            char c = s[i];
            if (c == '"') { i++; return sb.ToString(); }
            if (c == '\\')
            {
                i++;
                char e = s[i];
                switch (e)
                {
                    case 'n': sb.Append('\n'); break;
                    case 'r': sb.Append('\r'); break;
                    case 't': sb.Append('\t'); break;
                    case 'b': sb.Append('\b'); break;
                    case 'f': sb.Append('\f'); break;
                    case '\\': sb.Append('\\'); break;
                    case '/': sb.Append('/'); break;
                    case '"': sb.Append('"'); break;
                    case 'u':
                        sb.Append((char)Convert.ToInt32(s.Substring(i + 1, 4), 16));
                        i += 4;
                        break;
                    default: sb.Append(e); break;
                }
                i++;
            }
            else { sb.Append(c); i++; }
        }
        throw new Exception("json: unterminated string");
    }

    static object ParseNumber(string s, ref int i)
    {
        int start = i;
        while (i < s.Length && (char.IsDigit(s[i]) || s[i] == '-' || s[i] == '.' || s[i] == 'e' || s[i] == 'E' || s[i] == '+')) i++;
        string num = s.Substring(start, i - start);
        if (num.IndexOf('.') >= 0 || num.IndexOf('e') >= 0 || num.IndexOf('E') >= 0)
            return double.Parse(num, CultureInfo.InvariantCulture);
        return long.Parse(num, CultureInfo.InvariantCulture);
    }

    static string JsonSerialize(object o)
    {
        StringBuilder sb = new StringBuilder();
        WriteValue(sb, o);
        return sb.ToString();
    }

    static void WriteValue(StringBuilder sb, object o)
    {
        if (o == null) { sb.Append("null"); return; }
        if (o is string) { WriteString(sb, (string)o); return; }
        if (o is bool) { sb.Append((bool)o ? "true" : "false"); return; }
        if (o is long) { sb.Append(((long)o).ToString(CultureInfo.InvariantCulture)); return; }
        if (o is int) { sb.Append(((int)o).ToString(CultureInfo.InvariantCulture)); return; }
        if (o is double) { sb.Append(((double)o).ToString("R", CultureInfo.InvariantCulture)); return; }
        if (o is Dictionary<string, object>)
        {
            sb.Append('{');
            bool first = true;
            foreach (KeyValuePair<string, object> kv in (Dictionary<string, object>)o)
            {
                if (!first) sb.Append(',');
                first = false;
                WriteString(sb, kv.Key);
                sb.Append(':');
                WriteValue(sb, kv.Value);
            }
            sb.Append('}');
            return;
        }
        if (o is System.Collections.IList)
        {
            sb.Append('[');
            bool first = true;
            foreach (object item in (System.Collections.IList)o)
            {
                if (!first) sb.Append(',');
                first = false;
                WriteValue(sb, item);
            }
            sb.Append(']');
            return;
        }
        WriteString(sb, o.ToString());
    }

    static void WriteString(StringBuilder sb, string s)
    {
        sb.Append('"');
        foreach (char c in s)
        {
            switch (c)
            {
                case '"': sb.Append("\\\""); break;
                case '\\': sb.Append("\\\\"); break;
                case '\n': sb.Append("\\n"); break;
                case '\r': sb.Append("\\r"); break;
                case '\t': sb.Append("\\t"); break;
                default:
                    if (c < 0x20) sb.Append("\\u").Append(((int)c).ToString("x4"));
                    else sb.Append(c);
                    break;
            }
        }
        sb.Append('"');
    }

    // ---------- helpers ----------
    static long GetLong(Dictionary<string, object> d, string k, long def = 0)
    {
        object v;
        if (d != null && d.TryGetValue(k, out v) && v is long) return (long)v;
        return def;
    }

    static int GetInt(Dictionary<string, object> d, string k, int def = 0)
    {
        object v;
        // JSON numbers arrive as long (integers) or double (Python floats);
        // rejecting the latter silently dropped click coordinates to (0,0).
        if (d != null && d.TryGetValue(k, out v) && v is long) return (int)(long)v;
        if (d != null && d.TryGetValue(k, out v) && v is double) return (int)Math.Round((double)v);
        return def;
    }

    static string GetString(Dictionary<string, object> d, string k, string def = "")
    {
        object v;
        if (d != null && d.TryGetValue(k, out v) && v is string) return (string)v;
        return def;
    }

    static bool GetBool(Dictionary<string, object> d, string k)
    {
        object v;
        if (d != null && d.TryGetValue(k, out v) && v is bool) return (bool)v;
        return false;
    }

    static void Log(string msg)
    {
        try
        {
            File.AppendAllText(LogPath, DateTime.Now.ToString("HH:mm:ss.fff") + " " + msg + Environment.NewLine);
        }
        catch { }
    }
}
