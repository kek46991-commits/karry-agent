package ai.karry.observer

import android.os.Bundle
import android.text.format.DateUtils
import android.view.View
import android.widget.Button
import android.widget.EditText
import android.widget.TextView
import androidx.appcompat.app.AppCompatActivity
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.lifecycleScope
import androidx.lifecycle.repeatOnLifecycle
import kotlinx.coroutines.delay
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch

class MainActivity : AppCompatActivity() {
    private val api = KarryObserverApi()
    private lateinit var tokenStore: SecureTokenStore
    private lateinit var serverUrl: EditText
    private lateinit var ownerToken: EditText
    private lateinit var observerId: EditText
    private lateinit var operationId: EditText
    private lateinit var enrollButton: Button
    private lateinit var refreshButton: Button
    private lateinit var clearButton: Button
    private lateinit var connectionStatus: TextView
    private lateinit var actionList: TextView
    private lateinit var diagnosticList: TextView
    private var registration: Registration? = null

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_main)
        tokenStore = SecureTokenStore(this)
        bindViews()
        restoreRegistration()

        enrollButton.setOnClickListener { enrollObserver() }
        refreshButton.setOnClickListener { refresh() }
        clearButton.setOnClickListener { clearRegistration() }

        lifecycleScope.launch {
            repeatOnLifecycle(Lifecycle.State.STARTED) {
                while (isActive) {
                    if (registration != null) refresh(silent = true)
                    delay(5_000)
                }
            }
        }
    }

    private fun bindViews() {
        serverUrl = findViewById(R.id.serverUrl)
        ownerToken = findViewById(R.id.ownerToken)
        observerId = findViewById(R.id.observerId)
        operationId = findViewById(R.id.operationId)
        enrollButton = findViewById(R.id.enrollButton)
        refreshButton = findViewById(R.id.refreshButton)
        clearButton = findViewById(R.id.clearButton)
        connectionStatus = findViewById(R.id.connectionStatus)
        actionList = findViewById(R.id.actionList)
        diagnosticList = findViewById(R.id.diagnosticList)
    }

    private fun restoreRegistration() {
        registration = tokenStore.loadRegistration()
        registration?.let {
            serverUrl.setText(it.serverUrl)
            observerId.setText(it.observerId)
            operationId.setText(it.operationId)
            setStatus("登録済み: ${it.operationId} を観察中")
        } ?: run {
            serverUrl.setText("https://")
            observerId.setText("observer-android")
            operationId.setText("operation-iphone")
            setStatus("未登録")
        }
        updateControls()
    }

    private fun enrollObserver() {
        setBusy(true)
        lifecycleScope.launch {
            try {
                val result = api.enroll(
                    serverUrl.text.toString(),
                    ownerToken.text.toString().trim(),
                    observerId.text.toString().trim(),
                    operationId.text.toString().trim(),
                )
                tokenStore.saveRegistration(result)
                registration = result
                ownerToken.text.clear()
                setStatus("接続しました。iPhoneの状態を5秒ごとに更新します")
                updateControls()
                refresh()
            } catch (error: Exception) {
                setStatus("登録エラー: ${safeMessage(error)}")
            } finally {
                setBusy(false)
            }
        }
    }

    private fun refresh(silent: Boolean = false) {
        val current = registration ?: run {
            if (!silent) setStatus("先に観察端末を登録してください")
            return
        }
        if (!silent) setBusy(true)
        lifecycleScope.launch {
            try {
                render(api.snapshot(current))
                setStatus("接続中: ${current.operationId} を観察中")
            } catch (error: Exception) {
                setStatus("更新エラー: ${safeMessage(error)}")
            } finally {
                if (!silent) setBusy(false)
            }
        }
    }

    private fun render(snapshot: ObserverSnapshot) {
        actionList.text = if (snapshot.actions.isEmpty()) {
            "まだ操作はありません"
        } else {
            snapshot.actions.joinToString("\n\n") {
                val relative = DateUtils.getRelativeTimeSpanString(
                    (it.updatedAt * 1000).toLong(),
                    System.currentTimeMillis(),
                    DateUtils.MINUTE_IN_MILLIS,
                )
                "${actionLabel(it.action)}\n状態: ${statusLabel(it.status)} / $relative"
            }
        }
        diagnosticList.text = if (snapshot.diagnostics.isEmpty()) {
            "エラーはありません"
        } else {
            snapshot.diagnostics.joinToString("\n\n") {
                "${actionLabel(it.action)}: ${categoryLabel(it.category)}\n${recommendationLabel(it.recommendation)}"
            }
        }
    }

    private fun clearRegistration() {
        tokenStore.clear()
        registration = null
        ownerToken.text.clear()
        actionList.text = "まだ操作はありません"
        diagnosticList.text = "エラーはありません"
        setStatus("登録を解除しました")
        updateControls()
    }

    private fun setBusy(busy: Boolean) {
        enrollButton.isEnabled = !busy
        refreshButton.isEnabled = !busy && registration != null
    }

    private fun updateControls() {
        val registered = registration != null
        refreshButton.isEnabled = registered
        clearButton.visibility = if (registered) View.VISIBLE else View.GONE
    }

    private fun setStatus(message: String) {
        connectionStatus.text = message
    }

    private fun safeMessage(error: Exception): String =
        error.message?.take(160)?.replace(Regex("(?i)bearer\\s+\\S+"), "Bearer [非表示]")
            ?: "通信に失敗しました"

    private fun actionLabel(action: String): String = when (action) {
        "open_app" -> "アプリを開く"
        "open_url" -> "Webページを開く"
        "create_calendar_event" -> "予定を作成"
        "create_reminder" -> "リマインダーを作成"
        "request_location" -> "位置情報を取得"
        "choose_file" -> "ファイルを選択"
        "choose_photo" -> "写真を選択"
        "draft_message" -> "メッセージ下書き"
        else -> "不明な操作"
    }

    private fun statusLabel(status: String): String = when (status) {
        "awaiting_confirmation" -> "確認待ち"
        "pending" -> "実行待ち"
        "claimed" -> "iPhoneで実行中"
        "completed" -> "完了"
        "failed" -> "失敗"
        else -> "不明"
    }

    private fun categoryLabel(category: String): String = when (category) {
        "permission" -> "iPhoneの権限が必要です"
        "capability" -> "アプリまたは機能が利用できません"
        "transient" -> "一時的な通信エラーです"
        "input" -> "指示内容を確認してください"
        else -> "実行エラーです"
    }

    private fun recommendationLabel(recommendation: String): String = when (recommendation) {
        "request_the_required_ios_permission" -> "iPhone側で必要な許可をオンにしてください"
        "verify_app_intent_or_shortcut_availability" -> "対象アプリとショートカットを確認してください"
        "retry_after_connectivity_recovers" -> "通信が戻ってから再試行してください"
        "correct_the_action_payload" -> "操作内容を修正して再度送ってください"
        else -> "秘密情報を除いた実行ログを確認してください"
    }
}
