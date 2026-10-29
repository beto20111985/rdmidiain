package br.com.rdmidiaindoor.player.pop

import android.content.Context
import androidx.room.Dao
import androidx.room.Database
import androidx.room.Entity
import androidx.room.Index
import androidx.room.Insert
import androidx.room.OnConflictStrategy
import androidx.room.PrimaryKey
import androidx.room.Query
import androidx.room.Room
import androidx.room.RoomDatabase

@Entity(tableName = "pop_outbox", indices = [Index(value = ["seq"])])
data class OutboxEntity(
    @PrimaryKey val playbackUuid: String,
    val deviceId: String,
    val seq: Long,
    val eventJson: String,
    val signature: String,
    val createdAtMs: Long,
)

/** Estado da cadeia por device_id: último seq e hash do último evento (nunca reinicia em reboot). */
@Entity(tableName = "pop_chain")
data class ChainEntity(
    @PrimaryKey val deviceId: String,
    val lastSeq: Long,
    val lastHash: String,
)

@Dao
interface PopDao {
    @Query("SELECT * FROM pop_chain WHERE deviceId = :deviceId")
    fun chain(deviceId: String): ChainEntity?

    @Insert(onConflict = OnConflictStrategy.REPLACE)
    fun saveChain(chain: ChainEntity)

    @Insert(onConflict = OnConflictStrategy.ABORT)
    fun insert(row: OutboxEntity)

    @Query("SELECT * FROM pop_outbox ORDER BY seq ASC LIMIT :limit")
    fun pending(limit: Int): List<OutboxEntity>

    @Query("DELETE FROM pop_outbox WHERE playbackUuid IN (:ids)")
    fun delete(ids: List<String>)

    @Query("SELECT COUNT(*) FROM pop_outbox")
    fun count(): Int
}

@Database(entities = [OutboxEntity::class, ChainEntity::class], version = 1, exportSchema = false)
abstract class PopDatabase : RoomDatabase() {
    abstract fun dao(): PopDao

    companion object {
        fun open(context: Context): PopDatabase =
            Room.databaseBuilder(context.applicationContext, PopDatabase::class.java, "rd_pop.db")
                .setJournalMode(JournalMode.TRUNCATE) // durabilidade simples frente a queda de energia
                .build()
    }
}

/** [PopStore] sobre Room: incremento de seq + inserção do evento na MESMA transação. */
class RoomPopStore(private val db: PopDatabase) : PopStore {
    private val dao = db.dao()

    override fun append(deviceId: String, build: (ChainState) -> Pair<OutboxRow, ChainState>): OutboxRow =
        db.runInTransaction(java.util.concurrent.Callable {
            val current = dao.chain(deviceId)?.let { ChainState(it.deviceId, it.lastSeq, it.lastHash) }
                ?: ChainState.initial(deviceId)
            val (row, next) = build(current)
            dao.insert(OutboxEntity(row.playbackUuid, row.deviceId, row.seq, row.eventJson, row.signature, row.createdAtMs))
            dao.saveChain(ChainEntity(next.deviceId, next.lastSeq, next.lastHash))
            row
        })

    override fun chainState(deviceId: String): ChainState =
        dao.chain(deviceId)?.let { ChainState(it.deviceId, it.lastSeq, it.lastHash) } ?: ChainState.initial(deviceId)

    override fun pending(limit: Int): List<OutboxRow> =
        dao.pending(limit).map { OutboxRow(it.playbackUuid, it.deviceId, it.seq, it.eventJson, it.signature, it.createdAtMs) }

    override fun delete(playbackUuids: Collection<String>) {
        if (playbackUuids.isEmpty()) return
        playbackUuids.chunked(500).forEach { dao.delete(it) }
    }

    override fun count(): Int = dao.count()
}
