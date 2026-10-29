package br.com.rdmidiaindoor.player.core

/** Persistência local da âncora por tela (write-once local; só o servidor pode substituí-la). */
interface AnchorStore {
    fun get(screenId: String): Long?
    /** Grava se ausente; devolve o valor efetivo (o existente, se já houver). */
    fun setIfAbsent(screenId: String, anchorMs: Long): Long
    /** Adota o valor do servidor (write-once no servidor: ele devolve o existente). */
    fun adoptFromServer(screenId: String, anchorMs: Long)
    fun isPostedToServer(screenId: String): Boolean
    fun markPosted(screenId: String)
}

/**
 * Âncora do ciclo (RDPP §5.1 item 1; P001 §31; ADR_006 §3).
 * - manifesto com `anchor_ms` ⇒ autoridade do servidor (adotada e persistida);
 * - sem âncora em lugar nenhum ⇒ no PRIMEIRO START VÁLIDO, âncora = t arredondado para baixo ao segundo,
 *   gravada localmente e enviada por `POST /api/v1/player/anchor`;
 * - reboot nunca recalcula: a âncora local persistida é reutilizada.
 */
class AnchorManager(private val store: AnchorStore) {

    /** Âncora conhecida para a tela (manifesto > local). null ⇒ ainda não houve primeiro start válido. */
    fun known(screenId: String, manifestAnchorMs: Long?): Long? {
        if (manifestAnchorMs != null) {
            if (store.get(screenId) != manifestAnchorMs) store.adoptFromServer(screenId, manifestAnchorMs)
            return manifestAnchorMs
        }
        return store.get(screenId)
    }

    /** Primeiro start válido (manifesto ativado e primeira mídia pronta). Idempotente. */
    fun establish(screenId: String, manifestAnchorMs: Long?, correctedNowMs: Long): Long {
        known(screenId, manifestAnchorMs)?.let { return it }
        return store.setIfAbsent(screenId, floorToSecond(correctedNowMs))
    }

    /** Resposta do servidor ao POST (write-once): adota o valor devolvido. */
    fun onServerResponse(screenId: String, serverAnchorMs: Long) {
        store.adoptFromServer(screenId, serverAnchorMs)
        store.markPosted(screenId)
    }

    fun needsPost(screenId: String): Boolean = store.get(screenId) != null && !store.isPostedToServer(screenId)

    companion object {
        fun floorToSecond(ms: Long): Long = ms.floorDiv(1000L) * 1000L
    }
}

class InMemoryAnchorStore : AnchorStore {
    private val anchors = HashMap<String, Long>()
    private val posted = HashSet<String>()
    override fun get(screenId: String) = anchors[screenId]
    override fun setIfAbsent(screenId: String, anchorMs: Long): Long = anchors.getOrPut(screenId) { anchorMs }
    override fun adoptFromServer(screenId: String, anchorMs: Long) { anchors[screenId] = anchorMs }
    override fun isPostedToServer(screenId: String) = screenId in posted
    override fun markPosted(screenId: String) { posted += screenId }
}
