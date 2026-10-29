using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;
using System.Text.RegularExpressions;

namespace RdPlayer.Core.Bridge;

public enum BridgeKind
{
    Request,
    Notify,
}

public sealed record BridgeInbound(BridgeKind Kind, string? Id, string Method, JsonObject Params);

/// <summary>
/// Ponte JS ↔ nativo <c>rd-native/1</c> (player-windows/BRIDGE_PROPOSTA.md, aceita pelo coordenador como especificação).
/// Envelopes: request {rd:1,kind:"request",id,method,params} → response {rd:1,kind:"response",id,ok,result|error};
/// notify {rd:1,kind:"notify",method,params}; event {rd:1,kind:"event",event,data}.
/// </summary>
public static partial class BridgeProtocol
{
    public const string Name = "rd-native/1";
    public const int MaxMessageBytes = 64 * 1024;

    [GeneratedRegex("^[a-z][a-z0-9_]{0,39}$")]
    private static partial Regex MethodRx();

    [GeneratedRegex("^[A-Za-z0-9_-]{1,64}$")]
    private static partial Regex IdRx();

    public static bool TryParse(string? json, out BridgeInbound? msg, out string? error)
    {
        msg = null;
        error = null;
        if (string.IsNullOrEmpty(json)) { error = "vazio"; return false; }
        if (Encoding.UTF8.GetByteCount(json) > MaxMessageBytes) { error = "mensagem grande demais"; return false; }
        JsonNode? node;
        try { node = JsonNode.Parse(json); }
        catch (JsonException) { error = "JSON inválido"; return false; }
        if (node is not JsonObject o) { error = "objeto esperado"; return false; }
        if (o["rd"] is not JsonValue rdv || !rdv.TryGetValue<int>(out var rd) || rd != 1) { error = "rd != 1"; return false; }
        var kind = Str(o, "kind");
        var method = Str(o, "method");
        if (method is null || !MethodRx().IsMatch(method)) { error = "method inválido"; return false; }
        var p = o["params"];
        JsonObject prm;
        if (p is null) prm = new JsonObject();
        else if (p is JsonObject po) prm = (JsonObject)po.DeepClone();
        else { error = "params deve ser objeto"; return false; }

        switch (kind)
        {
            case "request":
                var id = Str(o, "id");
                if (id is null || !IdRx().IsMatch(id)) { error = "id inválido"; return false; }
                msg = new BridgeInbound(BridgeKind.Request, id, method, prm);
                return true;
            case "notify":
                msg = new BridgeInbound(BridgeKind.Notify, null, method, prm);
                return true;
            default:
                error = "kind inválido";
                return false;
        }
    }

    public static string Response(string id, JsonNode? result) => new JsonObject
    {
        ["rd"] = 1,
        ["kind"] = "response",
        ["id"] = id,
        ["ok"] = true,
        ["result"] = result ?? new JsonObject(),
    }.ToJsonString();

    public static string Error(string id, string code, string message) => new JsonObject
    {
        ["rd"] = 1,
        ["kind"] = "response",
        ["id"] = id,
        ["ok"] = false,
        ["error"] = new JsonObject { ["code"] = code, ["message"] = message },
    }.ToJsonString();

    public static string Event(string name, JsonNode? data) => new JsonObject
    {
        ["rd"] = 1,
        ["kind"] = "event",
        ["event"] = name,
        ["data"] = data ?? new JsonObject(),
    }.ToJsonString();

    /// <summary>
    /// Script injetado antes de qualquer script da página: define window.RD_NATIVE (congelado, não reconfigurável)
    /// e, opcionalmente, esconde o cursor via folha de estilo construída (não depende de 'unsafe-inline').
    /// </summary>
    public static string BuildInjectedScript(JsonObject nativeInfo, bool hideCursor)
    {
        var json = nativeInfo.ToJsonString();
        var sb = new StringBuilder();
        sb.Append("(()=>{try{const info=").Append(json).Append(';');
        sb.Append("const fz=o=>{for(const v of Object.values(o)){if(v&&typeof v==='object')fz(v);}return Object.freeze(o);};");
        sb.Append("if(!('RD_NATIVE' in window)){Object.defineProperty(window,'RD_NATIVE',{value:fz(info),writable:false,configurable:false,enumerable:false});}");
        sb.Append("}catch(e){}");
        if (hideCursor)
        {
            sb.Append("try{const s=new CSSStyleSheet();s.replaceSync('*,*::before,*::after{cursor:none!important}');");
            sb.Append("document.adoptedStyleSheets=[...document.adoptedStyleSheets,s];}catch(e){}");
        }
        sb.Append("})();");
        return sb.ToString();
    }

    public static string? Str(JsonObject o, string name) =>
        o[name] is JsonValue v && v.TryGetValue<string>(out var s) ? s : null;
}
