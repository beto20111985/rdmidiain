namespace RdPlayer.Core;

/// <summary>Códigos de saída da casca, interpretados pelo watchdog (redundância caso a mensagem do pipe se perca).</summary>
public static class ShellExitCodes
{
    /// <summary>Saída autorizada pelo menu técnico: o watchdog também encerra.</summary>
    public const int ExitRequested = 0;
    /// <summary>Falha genérica / exceção.</summary>
    public const int Crash = 1;
    /// <summary>Reinício solicitado (comando restart_app, menu técnico, política local): relançar já, sem contar como falha.</summary>
    public const int RestartRequested = 3;
    /// <summary>Outra instância já estava rodando.</summary>
    public const int AlreadyRunning = 4;
    /// <summary>Atualização preparada: o watchdog aplica a troca de slots.</summary>
    public const int ApplyUpdate = 5;
    /// <summary>Teste de fumaça reprovado.</summary>
    public const int SmokeFailed = 10;
    /// <summary>Teste de fumaça excedeu o tempo.</summary>
    public const int SmokeTimeout = 11;
    /// <summary>WebView2 Runtime ausente.</summary>
    public const int WebView2Missing = 20;
}
