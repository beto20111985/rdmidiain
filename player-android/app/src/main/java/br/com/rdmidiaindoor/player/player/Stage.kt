package br.com.rdmidiaindoor.player.player

import android.content.Context
import android.graphics.Bitmap
import android.graphics.BitmapFactory
import android.graphics.Color
import android.graphics.ImageDecoder
import android.graphics.drawable.AnimatedImageDrawable
import android.graphics.drawable.Drawable
import android.os.Build
import android.os.Handler
import android.os.Looper
import android.view.Gravity
import android.view.LayoutInflater
import android.view.View
import android.widget.FrameLayout
import android.widget.ImageView
import android.widget.LinearLayout
import android.widget.TextView
import androidx.annotation.OptIn
import androidx.media3.common.MediaItem
import androidx.media3.common.PlaybackException
import androidx.media3.common.Player
import androidx.media3.common.util.UnstableApi
import androidx.media3.exoplayer.ExoPlayer
import androidx.media3.ui.AspectRatioFrameLayout
import androidx.media3.ui.PlayerView
import br.com.rdmidiaindoor.player.R
import java.io.File
import java.util.concurrent.Executors

/** O que uma camada deve exibir. [file] null ⇒ tela institucional embutida (sem PoP). */
data class DisplayItem(val id: String, val file: File?, val isVideo: Boolean, val mime: String?)

/**
 * Camada de mídia (uma das duas do double buffer): imagem (ImageView, FIT_CENTER = letterbox, nunca esticar),
 * vídeo (Media3 ExoPlayer em TextureView — permite fade por alpha; RESIZE_MODE_FIT) ou tela institucional.
 * Conteúdo distribuído sem áudio: volume 0.
 */
@OptIn(UnstableApi::class)
class MediaLayer(private val context: Context, private val decodeExecutor: java.util.concurrent.Executor) {
    val root = FrameLayout(context).apply { setBackgroundColor(Color.BLACK) }
    private val image = ImageView(context).apply {
        scaleType = ImageView.ScaleType.FIT_CENTER
        visibility = View.GONE
    }
    private val video: PlayerView = (LayoutInflater.from(context).inflate(R.layout.layer_video, root, false) as PlayerView).apply {
        useController = false
        resizeMode = AspectRatioFrameLayout.RESIZE_MODE_FIT
        visibility = View.GONE
    }
    private val placeholder: View = LinearLayout(context).apply {
        orientation = LinearLayout.VERTICAL
        gravity = Gravity.CENTER
        setBackgroundColor(Color.rgb(12, 18, 32))
        addView(TextView(context).apply {
            text = context.getString(R.string.brand_name)
            setTextColor(Color.WHITE)
            textSize = 42f
            gravity = Gravity.CENTER
        })
        addView(TextView(context).apply {
            text = context.getString(R.string.brand_tagline)
            setTextColor(Color.rgb(170, 190, 220))
            textSize = 20f
            gravity = Gravity.CENTER
        })
        visibility = View.GONE
    }
    private val main = Handler(Looper.getMainLooper())
    private var exo: ExoPlayer? = null
    private var listener: Player.Listener? = null
    private var generation = 0

    /** Muda a cada prepare/release — permite saber se a camada foi reaproveitada durante um fade. */
    val currentGeneration: Int get() = generation

    /** Callbacks de reprodução (re-vinculados pelo controlador quando a camada vira a da frente). */
    @Volatile var onEnded: () -> Unit = {}
    @Volatile var onError: (String) -> Unit = {}

    var loadedId: String? = null
        private set
    var ready = false
        private set
    var isVideo = false
        private set

    init {
        root.addView(image, FrameLayout.LayoutParams(-1, -1))
        root.addView(video, FrameLayout.LayoutParams(-1, -1))
        root.addView(placeholder, FrameLayout.LayoutParams(-1, -1))
    }

    /** Prepara o item (sem exibir). [onReady] só é chamado com o primeiro quadro pronto (evita quadro preto). */
    fun prepare(item: DisplayItem, targetW: Int, targetH: Int, onReady: () -> Unit, onPrepareError: (String) -> Unit) {
        val gen = ++generation
        reset()
        onEnded = {}
        onError = {}
        loadedId = item.id
        isVideo = item.isVideo && item.file != null
        val file = item.file
        when {
            file == null -> {
                placeholder.visibility = View.VISIBLE
                ready = true
                onReady()
            }
            item.isVideo -> prepareVideo(file, gen, onReady, onPrepareError)
            else -> prepareImage(file, item.mime, targetW, targetH, gen, onReady, onPrepareError)
        }
    }

    private fun prepareImage(file: File, mime: String?, w: Int, h: Int, gen: Int, onReady: () -> Unit, onError: (String) -> Unit) {
        decodeExecutor.execute {
            val result: Any? = try {
                if (mime == "image/gif" && Build.VERSION.SDK_INT >= Build.VERSION_CODES.P) {
                    ImageDecoder.decodeDrawable(ImageDecoder.createSource(file))
                } else decodeSampled(file, w, h)
            } catch (t: Throwable) {
                null
            }
            main.post {
                if (gen != generation) return@post
                when (result) {
                    is Bitmap -> image.setImageBitmap(result)
                    is Drawable -> {
                        image.setImageDrawable(result)
                        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.P && result is AnimatedImageDrawable) result.start()
                    }
                    else -> {
                        onError("IMAGE_DECODE_FAILED")
                        return@post
                    }
                }
                image.visibility = View.VISIBLE
                ready = true
                onReady()
            }
        }
    }

    private fun prepareVideo(file: File, gen: Int, onReady: () -> Unit, onPrepareError: (String) -> Unit) {
        val player = exo ?: ExoPlayer.Builder(context).build().also {
            it.volume = 0f
            it.repeatMode = Player.REPEAT_MODE_OFF
            exo = it
        }
        video.player = player
        video.visibility = View.VISIBLE
        val l = object : Player.Listener {
            override fun onRenderedFirstFrame() {
                if (gen != generation || ready) return
                ready = true
                onReady()
            }

            override fun onPlaybackStateChanged(state: Int) {
                if (gen == generation && state == Player.STATE_ENDED) onEnded()
            }

            override fun onPlayerError(error: PlaybackException) {
                if (gen != generation) return
                val reason = "VIDEO_${error.errorCodeName}"
                if (ready) onError(reason) else onPrepareError(reason)
            }
        }
        listener = l
        player.addListener(l)
        player.setMediaItem(MediaItem.fromUri(android.net.Uri.fromFile(file)))
        player.playWhenReady = false
        player.prepare()
    }

    fun start() {
        exo?.takeIf { isVideo }?.playWhenReady = true
    }

    /** Posição atual do vídeo (watchdog de posição congelada). */
    fun videoPositionMs(): Long? = if (isVideo) exo?.currentPosition else null

    fun reset() {
        ready = false
        listener?.let { exo?.removeListener(it) }
        listener = null
        exo?.stop()
        exo?.clearMediaItems()
        image.setImageDrawable(null)
        image.visibility = View.GONE
        video.visibility = View.GONE
        placeholder.visibility = View.GONE
    }

    fun release() {
        generation++
        reset()
        video.player = null
        exo?.release()
        exo = null
        loadedId = null
    }

    companion object {
        /** Decodifica com inSampleSize (≥ tamanho alvo) — evita OOM em TV Box com imagens grandes. */
        fun decodeSampled(file: File, reqW: Int, reqH: Int): Bitmap? {
            val bounds = BitmapFactory.Options().apply { inJustDecodeBounds = true }
            BitmapFactory.decodeFile(file.absolutePath, bounds)
            var sample = 1
            if (reqW > 0 && reqH > 0) {
                while (bounds.outWidth / (sample * 2) >= reqW && bounds.outHeight / (sample * 2) >= reqH) sample *= 2
            }
            return BitmapFactory.decodeFile(file.absolutePath, BitmapFactory.Options().apply { inSampleSize = sample })
        }
    }
}

/**
 * Palco com double buffer: a camada de trás é preparada (pré-carga do próximo item) enquanto a da frente exibe;
 * na troca a de trás vem para frente com fade-in de `fade_ms` SOBRE a anterior (que continua visível por baixo) —
 * nunca há quadro preto entre itens.
 */
class Stage(context: Context, val container: FrameLayout) {
    private val executor = Executors.newSingleThreadExecutor { r -> Thread(r, "rd-image-decode").apply { isDaemon = true } }
    private val layers = arrayOf(MediaLayer(context, executor), MediaLayer(context, executor))
    private var frontIdx = 0

    val front: MediaLayer get() = layers[frontIdx]
    val back: MediaLayer get() = layers[1 - frontIdx]

    init {
        layers.forEach { container.addView(it.root, FrameLayout.LayoutParams(-1, -1)) }
        back.root.alpha = 0f
        back.root.visibility = View.INVISIBLE
    }

    /** Traz a camada de trás para frente com fade. [onSwapped] é chamado no INÍCIO visível (início do PoP). */
    fun swap(fadeMs: Long, onSwapped: () -> Unit, onFadeDone: () -> Unit) {
        val incoming = back
        val outgoing = front
        val outgoingGen = outgoing.currentGeneration
        frontIdx = 1 - frontIdx
        incoming.root.bringToFront()
        incoming.root.visibility = View.VISIBLE
        incoming.root.alpha = if (fadeMs > 0) 0f else 1f
        incoming.start()
        onSwapped()
        val done = Runnable {
            outgoing.root.visibility = View.INVISIBLE
            outgoing.root.alpha = 0f
            // só limpa se a camada não foi reaproveitada (nova pré-carga) durante o fade
            if (outgoing.currentGeneration == outgoingGen) outgoing.reset()
            onFadeDone()
        }
        if (fadeMs > 0) {
            incoming.root.animate().alpha(1f).setDuration(fadeMs).withEndAction(done).start()
        } else {
            done.run()
        }
    }

    fun releaseAll() {
        layers.forEach { it.release() }
    }
}
