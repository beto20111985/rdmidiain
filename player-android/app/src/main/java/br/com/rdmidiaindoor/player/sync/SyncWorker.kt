package br.com.rdmidiaindoor.player.sync

import android.content.Context
import androidx.work.BackoffPolicy
import androidx.work.Constraints
import androidx.work.CoroutineWorker
import androidx.work.ExistingWorkPolicy
import androidx.work.NetworkType
import androidx.work.OneTimeWorkRequestBuilder
import androidx.work.WorkManager
import androidx.work.WorkerParameters
import br.com.rdmidiaindoor.player.PlayerApp
import java.util.concurrent.TimeUnit

/**
 * "Gatilho" do WorkManager: quando a sincronização falha por rede, agenda um trabalho único com restrição
 * NETWORK=CONNECTED e backoff exponencial; ao rodar, delega ao runtime (que vive no processo do app).
 * Não é polling: só existe enquanto há uma falha pendente (P001 §70 — retries/backoff permitidos).
 */
class SyncWorker(context: Context, params: WorkerParameters) : CoroutineWorker(context, params) {
    override suspend fun doWork(): Result {
        val app = applicationContext as? PlayerApp ?: return Result.failure()
        val outcome = app.runtime.syncNow("worker")
        return if (outcome is SyncOutcome.Failed && outcome.reason.startsWith("FETCH")) Result.retry() else Result.success()
    }

    companion object {
        private const val NAME = "rd_sync_retry"

        fun enqueue(context: Context) {
            val req = OneTimeWorkRequestBuilder<SyncWorker>()
                .setConstraints(Constraints.Builder().setRequiredNetworkType(NetworkType.CONNECTED).build())
                .setBackoffCriteria(BackoffPolicy.EXPONENTIAL, 30, TimeUnit.SECONDS)
                .setInitialDelay(10, TimeUnit.SECONDS)
                .build()
            WorkManager.getInstance(context).enqueueUniqueWork(NAME, ExistingWorkPolicy.KEEP, req)
        }
    }
}
