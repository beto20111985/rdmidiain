using System;
using System.Runtime.InteropServices;
using RdPlayer.Core.Input;

namespace RdPlayer.Shell;

/// <summary>
/// Hook de teclado de baixo nível (opcional, kiosk.block_system_keys): bloqueia tecla Windows, Alt+Tab, Alt+Esc e Ctrl+Esc.
/// Ctrl+Alt+Del não pode (nem deve) ser bloqueado — é a saída de emergência do Windows.
/// </summary>
internal sealed class KeyboardHook : IDisposable
{
    private readonly Native.LowLevelKeyboardProc _proc; // referência mantida para o GC não coletar o delegate
    private readonly Func<bool> _isEnabled;
    private IntPtr _hook;

    public KeyboardHook(Func<bool> isEnabled)
    {
        _isEnabled = isEnabled;
        _proc = HookProc;
    }

    public bool Install()
    {
        if (_hook != IntPtr.Zero) return true;
        _hook = Native.SetWindowsHookEx(Native.WH_KEYBOARD_LL, _proc, Native.GetModuleHandle(null), 0);
        return _hook != IntPtr.Zero;
    }

    public int LastError => Marshal.GetLastWin32Error();

    private IntPtr HookProc(int nCode, IntPtr wParam, IntPtr lParam)
    {
        if (nCode >= 0 && _isEnabled())
        {
            int msg = (int)wParam;
            if (msg is Native.WM_KEYDOWN or Native.WM_SYSKEYDOWN)
            {
                var k = Marshal.PtrToStructure<Native.KBDLLHOOKSTRUCT>(lParam);
                bool alt = (k.flags & Native.LLKHF_ALTDOWN) != 0;
                bool ctrl = (Native.GetKeyState(Vk.Control) & 0x8000) != 0;
                if (ShortcutPolicy.IsBlockedSystemKey((int)k.vkCode, ctrl, alt)) return new IntPtr(1);
            }
        }
        return Native.CallNextHookEx(_hook, nCode, wParam, lParam);
    }

    public void Dispose()
    {
        if (_hook != IntPtr.Zero)
        {
            Native.UnhookWindowsHookEx(_hook);
            _hook = IntPtr.Zero;
        }
    }
}
