using System.Text.Json;
using System.Text.Json.Serialization;
using RdPlayer.Core.Update;

namespace RdPlayer.Core.Config;

/// <summary>Serialização por source generator (compatível com trimming, sem reflexão).</summary>
[JsonSourceGenerationOptions(
    PropertyNamingPolicy = JsonKnownNamingPolicy.SnakeCaseLower,
    ReadCommentHandling = JsonCommentHandling.Skip,
    AllowTrailingCommas = true,
    WriteIndented = true,
    DefaultIgnoreCondition = JsonIgnoreCondition.Never)]
[JsonSerializable(typeof(PlayerConfig))]
[JsonSerializable(typeof(UpdateState))]
[JsonSerializable(typeof(ShellState))]
public partial class CoreJsonContext : JsonSerializerContext
{
}
