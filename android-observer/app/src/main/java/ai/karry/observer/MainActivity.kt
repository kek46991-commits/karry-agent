package ai.karry.observer

import android.graphics.Color
import android.graphics.drawable.GradientDrawable
import android.os.Bundle
import android.text.Editable
import android.text.TextWatcher
import android.text.format.DateUtils
import android.view.Gravity
import android.view.View
import android.view.ViewGroup
import android.widget.Button
import android.widget.EditText
import android.widget.ImageView
import android.widget.LinearLayout
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
    private lateinit var setupLog: TextView
    private lateinit var setupPrompt: TextView
    private lateinit var setupPromptRow: View
    private var backendReachable: Boolean? = null
    private var setupSkipped = false
    private lateinit var liveCard: TextView
    private lateinit var screenStatus: TextView
    private lateinit var screenPreview: ImageView
    private lateinit var actionContainer: LinearLayout
    private lateinit var diagnosticContainer: LinearLayout
    private var registration: Registration? = null
    private var blink = false

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_main)
        tokenStore = SecureTokenStore(this)
        bindViews()
        restoreRegistration()

        enrollButton.setOnClickListener { enrollObserver() }
        refreshButton.setOnClickListener { refresh() }
        clearButton.setOnClickListener { clearRegistration() }
        findViewById<Button>(R.id.setupYes).setOnClickListener { enrollObserver() }
        findViewById<Button>(R.id.setupNo).setOnClickListener {
            setupSkipped = true
            renderSetup()
        }
        val watcher = object : TextWatcher {
            override fun beforeTextChanged(s: CharSequence?, start: Int, count: Int, after: Int) = Unit
            override fun onTextChanged(s: CharSequence?, start: Int, before: Int, count: Int) = Unit
            override fun afterTextChanged(s: Editable?) = renderSetup()
        }
        ownerToken.addTextChangedListener(watcher)
        serverUrl.addTextChangedListener(watcher)
        checkBackend()

        lifecycleScope.launch {
            repeatOnLifecycle(Lifecycle.State.STARTED) {
                while (isActive) {
                    if (registration != null) refresh(silent = true)
                    delay(2_000)
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
        setupLog = findViewById(R.id.setupLog)
        setupPrompt = findViewById(R.id.setupPrompt)
        setupPromptRow = findViewById(R.id.setupPromptRow)
        liveCard = findViewById(R.id.liveCard)
        screenStatus = findViewById(R.id.screenStatus)
        screenPreview = findViewById(R.id.screenPreview)
        actionContainer = findViewById(R.id.actionContainer)
        diagnosticContainer = findViewById(R.id.diagnosticContainer)
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
            observerId.setText(BuildConfig.DEFAULT_OBSERVER_ID)
            operationId.setText(BuildConfig.DEFAULT_OPERATION_ID)
            setStatus("未登録")
        }
        updateControls()
        renderSetup()
    }

    private fun setupSnapshot() = SetupStatus.Snapshot(
        appName = getString(R.string.app_name),
        applicationId = BuildConfig.APPLICATION_ID,
        serverUrlValid = runCatching { KarryObserverApi.validateServerUrl(serverUrl.text.toString()) }.isSuccess,
        ownerTokenEntered = ownerToken.text.toString().isNotBlank(),
        enrolled = registration != null,
        backendReachable = backendReachable,
        secureStorage = tokenStore.isAvailable(),
    )

    private fun renderSetup() {
        val snapshot = setupSnapshot()
        setupLog.text = SetupStatus.lines(snapshot).joinToString("\n")
        val prompt = SetupStatus.prompt(snapshot, setupSkipped)
        setupPrompt.text = prompt ?: ""
        setupPrompt.visibility = if (prompt == null) View.GONE else View.VISIBLE
        setupPromptRow.visibility = if (prompt == "enroll: register this observer now?") View.VISIBLE else View.GONE
    }

    private fun checkBackend() {
        val url = registration?.serverUrl ?: serverUrl.text.toString()
        lifecycleScope.launch {
            backendReachable = api.reachable(url)
            renderSetup()
        }
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
                setStatus("接続しました。iPhoneの状態を2秒ごとに更新します")
                updateControls()
                backendReachable = true
                renderSetup()
                refresh()
            } catch (error: Exception) {
                setStatus("登録エラー: ${safeMessage(error)}")
                checkBackend()
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
                blink = !blink
                setStatus("${if (blink) "● " else "○ "}接続中: ${current.operationId} を観察中")
            } catch (error: Exception) {
                setStatus("更新エラー: ${safeMessage(error)}")
            } finally {
                if (!silent) setBusy(false)
            }
        }
    }

    private fun render(snapshot: ObserverSnapshot) {
        renderScreen(snapshot.screen)
        renderLiveCard(snapshot.actions)
        renderActions(snapshot.actions)
        renderDiagnostics(snapshot.diagnostics)
    }

    private fun renderScreen(screen: ScreenPreview) {
        if (screen.sharing && screen.bitmap != null) {
            screenStatus.text = "● iPhone画面を共有中（2秒ごとに更新）"
            screenStatus.setTextColor(Color.parseColor("#4ADE80"))
            screenPreview.setImageBitmap(screen.bitmap)
            screenPreview.visibility = View.VISIBLE
        } else {
            screenStatus.text = "○ iPhone側で画面共有を開始してください"
            screenStatus.setTextColor(Color.parseColor("#FBBF24"))
            screenPreview.setImageDrawable(null)
            screenPreview.visibility = View.GONE
        }
    }

    private fun renderLiveCard(actions: List<ActionStatus>) {
        val active = actions.firstOrNull { it.status == "claimed" }
            ?: actions.firstOrNull { it.status == "pending" }
            ?: actions.firstOrNull { it.status == "awaiting_confirmation" }
        if (active == null) {
            liveCard.text = "● 待機中（実行中の操作はありません）"
            liveCard.setBackgroundColor(Color.parseColor("#1E293B"))
            liveCard.setTextColor(Color.parseColor("#E2E8F0"))
            return
        }
        val color = statusColor(active.status)
        liveCard.text = "● ${actionLabel(active.action)}\n${statusLabel(active.status)}"
        liveCard.setBackgroundColor(withAlpha(color, 0x33))
        liveCard.setTextColor(Color.parseColor("#F8FAFC"))
    }

    private fun renderActions(actions: List<ActionStatus>) {
        actionContainer.removeAllViews()
        if (actions.isEmpty()) {
            actionContainer.addView(emptyCard("まだ操作はありません"))
            return
        }
        actions.forEach { item ->
            val relative = DateUtils.getRelativeTimeSpanString(
                (item.updatedAt * 1000).toLong(),
                System.currentTimeMillis(),
                DateUtils.MINUTE_IN_MILLIS,
            )
            actionContainer.addView(
                timelineRow(
                    statusColor(item.status),
                    actionLabel(item.action),
                    "${statusLabel(item.status)} ・ $relative",
                ),
            )
        }
    }

    private fun renderDiagnostics(diagnostics: List<DiagnosticStatus>) {
        diagnosticContainer.removeAllViews()
        if (diagnostics.isEmpty()) {
            diagnosticContainer.addView(emptyCard("エラーはありません"))
            return
        }
        diagnostics.forEach { item ->
            diagnosticContainer.addView(
                timelineRow(
                    Color.parseColor("#F97316"),
                    "${actionLabel(item.action)}: ${categoryLabel(item.category)}",
                    recommendationLabel(item.recommendation),
                ),
            )
        }
    }

    private fun timelineRow(accent: Int, title: String, subtitle: String): View {
        val row = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            background = cardBackground()
            setPadding(dp(14), dp(12), dp(14), dp(12))
            layoutParams = LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT,
                ViewGroup.LayoutParams.WRAP_CONTENT,
            ).apply { bottomMargin = dp(8) }
        }
        val chip = View(this).apply {
            background = GradientDrawable().apply {
                shape = GradientDrawable.OVAL
                setColor(accent)
            }
            layoutParams = LinearLayout.LayoutParams(dp(12), dp(12)).apply {
                topMargin = dp(5)
                marginEnd = dp(12)
            }
        }
        val text = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            layoutParams = LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f)
        }
        text.addView(TextView(this).apply {
            setText(title)
            setTextColor(Color.parseColor("#F8FAFC"))
            textSize = 15f
            setTypeface(typeface, android.graphics.Typeface.BOLD)
        })
        text.addView(TextView(this).apply {
            setText(subtitle)
            setTextColor(Color.parseColor("#94A3B8"))
            textSize = 13f
        })
        row.addView(chip)
        row.addView(text)
        return row
    }

    private fun emptyCard(message: String): View = TextView(this).apply {
        setText(message)
        setTextColor(Color.parseColor("#94A3B8"))
        background = cardBackground()
        gravity = Gravity.CENTER_VERTICAL
        setPadding(dp(14), dp(16), dp(14), dp(16))
        layoutParams = LinearLayout.LayoutParams(
            ViewGroup.LayoutParams.MATCH_PARENT,
            ViewGroup.LayoutParams.WRAP_CONTENT,
        ).apply { bottomMargin = dp(8) }
    }

    private fun cardBackground(): GradientDrawable = GradientDrawable().apply {
        setColor(Color.parseColor("#1E293B"))
        cornerRadius = dp(10).toFloat()
    }

    private fun clearRegistration() {
        tokenStore.clear()
        registration = null
        ownerToken.text.clear()
        actionContainer.removeAllViews()
        diagnosticContainer.removeAllViews()
        actionContainer.addView(emptyCard("まだ操作はありません"))
        diagnosticContainer.addView(emptyCard("エラーはありません"))
        liveCard.text = "● 待機中"
        liveCard.setBackgroundColor(Color.parseColor("#1E293B"))
        liveCard.setTextColor(Color.parseColor("#E2E8F0"))
        setStatus("登録を解除しました")
        updateControls()
        setupSkipped = false
        renderSetup()
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

    private fun dp(value: Int): Int = (value * resources.displayMetrics.density).toInt()

    private fun withAlpha(color: Int, alpha: Int): Int =
        Color.argb(alpha, Color.red(color), Color.green(color), Color.blue(color))

    private fun statusColor(status: String): Int = when (status) {
        "awaiting_confirmation" -> Color.parseColor("#F59E0B")
        "pending" -> Color.parseColor("#64748B")
        "claimed" -> Color.parseColor("#3B82F6")
        "completed" -> Color.parseColor("#22C55E")
        "failed" -> Color.parseColor("#EF4444")
        else -> Color.parseColor("#94A3B8")
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
