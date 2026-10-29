using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;

namespace RdPlayer.Core.Json;

public sealed class CanonicalJsonException : Exception
{
    public CanonicalJsonException(string message) : base(message) { }
}

/// <summary>
/// JSON canônico RDJ1 (RDPP/1 §0): chaves ordenadas por ponto de código, sem espaços,
/// escapes mínimos, somente inteiros (float proibido), não-ASCII sem escape.
/// </summary>
public static class Rdj1
{
    public static string Canonicalize(string json)
    {
        JsonDocument doc;
        try
        {
            doc = JsonDocument.Parse(json, new JsonDocumentOptions { AllowTrailingCommas = false, CommentHandling = JsonCommentHandling.Disallow });
        }
        catch (JsonException ex)
        {
            throw new CanonicalJsonException("JSON inválido: " + ex.Message);
        }
        using (doc)
        {
            var sb = new StringBuilder(json.Length);
            Write(sb, doc.RootElement);
            return sb.ToString();
        }
    }

    public static string Canonicalize(JsonNode? node) => Canonicalize(node is null ? "null" : node.ToJsonString());

    public static byte[] CanonicalBytes(JsonNode? node) => Encoding.UTF8.GetBytes(Canonicalize(node));

    private static void Write(StringBuilder sb, JsonElement e)
    {
        switch (e.ValueKind)
        {
            case JsonValueKind.Object:
                {
                    var props = new List<KeyValuePair<string, JsonElement>>();
                    var seen = new HashSet<string>(StringComparer.Ordinal);
                    foreach (var p in e.EnumerateObject())
                    {
                        if (!seen.Add(p.Name)) throw new CanonicalJsonException("chave duplicada: " + p.Name);
                        props.Add(new(p.Name, p.Value));
                    }
                    props.Sort((a, b) => CompareCodePoints(a.Key, b.Key));
                    sb.Append('{');
                    for (int i = 0; i < props.Count; i++)
                    {
                        if (i > 0) sb.Append(',');
                        WriteString(sb, props[i].Key);
                        sb.Append(':');
                        Write(sb, props[i].Value);
                    }
                    sb.Append('}');
                    break;
                }
            case JsonValueKind.Array:
                {
                    sb.Append('[');
                    bool first = true;
                    foreach (var item in e.EnumerateArray())
                    {
                        if (!first) sb.Append(',');
                        first = false;
                        Write(sb, item);
                    }
                    sb.Append(']');
                    break;
                }
            case JsonValueKind.String:
                {
                    string s;
                    try { s = e.GetString()!; }
                    catch (InvalidOperationException) { throw new CanonicalJsonException("string com UTF-16 inválido"); }
                    WriteString(sb, s);
                    break;
                }
            case JsonValueKind.Number:
                {
                    var raw = e.GetRawText();
                    if (!IsJsonInteger(raw)) throw new CanonicalJsonException("número não inteiro proibido em RDJ1: " + raw);
                    sb.Append(raw == "-0" ? "0" : raw);
                    break;
                }
            case JsonValueKind.True: sb.Append("true"); break;
            case JsonValueKind.False: sb.Append("false"); break;
            case JsonValueKind.Null: sb.Append("null"); break;
            default: throw new CanonicalJsonException("tipo JSON não suportado: " + e.ValueKind);
        }
    }

    internal static bool IsJsonInteger(string raw)
    {
        if (raw.Length == 0) return false;
        int i = 0;
        if (raw[0] == '-') { i = 1; if (raw.Length == 1) return false; }
        if (raw[i] == '0') return i == raw.Length - 1;
        for (; i < raw.Length; i++) if (raw[i] < '0' || raw[i] > '9') return false;
        return true;
    }

    internal static int CompareCodePoints(string a, string b)
    {
        var ea = a.EnumerateRunes();
        var eb = b.EnumerateRunes();
        while (true)
        {
            bool ha = ea.MoveNext();
            bool hb = eb.MoveNext();
            if (!ha || !hb) return ha == hb ? 0 : (ha ? 1 : -1);
            int c = ea.Current.Value.CompareTo(eb.Current.Value);
            if (c != 0) return c;
        }
    }

    internal static void WriteString(StringBuilder sb, string s)
    {
        sb.Append('"');
        for (int i = 0; i < s.Length; i++)
        {
            char c = s[i];
            switch (c)
            {
                case '"': sb.Append("\\\""); break;
                case '\\': sb.Append("\\\\"); break;
                case '\b': sb.Append("\\b"); break;
                case '\f': sb.Append("\\f"); break;
                case '\n': sb.Append("\\n"); break;
                case '\r': sb.Append("\\r"); break;
                case '\t': sb.Append("\\t"); break;
                default:
                    if (c < 0x20)
                    {
                        sb.Append("\\u").Append(((int)c).ToString("x4"));
                    }
                    else if (char.IsHighSurrogate(c))
                    {
                        if (i + 1 < s.Length && char.IsLowSurrogate(s[i + 1])) { sb.Append(c).Append(s[i + 1]); i++; }
                        else throw new CanonicalJsonException("surrogate isolado");
                    }
                    else if (char.IsLowSurrogate(c))
                    {
                        throw new CanonicalJsonException("surrogate isolado");
                    }
                    else sb.Append(c);
                    break;
            }
        }
        sb.Append('"');
    }
}
