package br.com.vulcan.cptochat

import android.annotation.SuppressLint
import android.app.Activity
import android.content.Intent
import android.graphics.Bitmap
import android.net.Uri
import android.net.VpnService
import android.os.Bundle
import android.view.View
import android.webkit.ConsoleMessage
import android.webkit.PermissionRequest
import android.webkit.SslErrorHandler
import android.webkit.WebResourceRequest
import android.webkit.WebResourceResponse
import android.webkit.WebView
import android.webkit.WebViewClient
import android.webkit.WebChromeClient
import android.net.http.SslError
import android.webkit.WebSettings
import android.widget.Button
import android.widget.TextView
import android.widget.Toast
import androidx.activity.OnBackPressedCallback
import androidx.activity.result.contract.ActivityResultContracts
import androidx.webkit.WebViewCompat
import androidx.appcompat.app.AppCompatActivity
import androidx.webkit.WebSettingsCompat
import androidx.webkit.WebViewFeature
import com.wireguard.android.backend.GoBackend
import com.wireguard.android.backend.Tunnel
import com.wireguard.config.Config
import org.json.JSONObject
import java.util.concurrent.Executors

class MainActivity : AppCompatActivity() {
    private lateinit var webView: WebView
    private lateinit var errorView: TextView
    private lateinit var origin: Uri

    // WireGuard: the userspace backend owns the tunnel; the library AAR declares
    // the VpnService in the merged manifest, so no extra service class is needed.
    private lateinit var vpnBar: View
    private lateinit var vpnButton: Button
    private lateinit var vpnStatus: TextView
    private lateinit var backend: GoBackend
    private val vpnExecutor = Executors.newSingleThreadExecutor()
    private var vpnState = Tunnel.State.DOWN
    private val tunnel = object : Tunnel {
        override fun getName(): String = "crptochat"
        override fun onStateChange(newState: Tunnel.State) {
            vpnState = newState
            runOnUiThread { updateVpnUi(newState) }
        }
    }

    private val vpnPermissionLauncher = registerForActivityResult(
        ActivityResultContracts.StartActivityForResult()
    ) { result ->
        if (result.resultCode == Activity.RESULT_OK) {
            openConfigPicker()
        } else {
            Toast.makeText(this, R.string.vpn_permission_denied, Toast.LENGTH_SHORT).show()
        }
    }

    private val configPickerLauncher = registerForActivityResult(
        ActivityResultContracts.GetContent()
    ) { uri ->
        if (uri == null) return@registerForActivityResult
        try {
            val stream = contentResolver.openInputStream(uri)
                ?: throw IllegalArgumentException("stream indisponível")
            stream.use { Config.parse(it) }.let(::connectVpn)
        } catch (e: Exception) {
            Toast.makeText(this, R.string.vpn_config_error, Toast.LENGTH_LONG).show()
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_main)
        webView = findViewById(R.id.chat_webview)
        errorView = findViewById(R.id.error_message)
        vpnBar = findViewById(R.id.vpn_bar)
        vpnButton = findViewById(R.id.vpn_button)
        vpnStatus = findViewById(R.id.vpn_status)
        backend = GoBackend(applicationContext)
        vpnButton.setOnClickListener { onVpnButtonClicked() }
        updateVpnUi(vpnState)

        val configured = Uri.parse(BuildConfig.CHAT_ORIGIN)
        if ((configured.scheme != "https" && configured.scheme != "http") || configured.host.isNullOrBlank()) {
            showConfigurationError()
            return
        }
        origin = configured
        configureWebView()
        webView.loadUrl(origin.toString())

        onBackPressedDispatcher.addCallback(this, object : OnBackPressedCallback(true) {
            override fun handleOnBackPressed() {
                if (webView.canGoBack()) webView.goBack() else finish()
            }
        })
    }

    @SuppressLint("SetJavaScriptEnabled")
    private fun configureWebView() {
        webView.settings.apply {
            javaScriptEnabled = true // Required by the Crptochat browser client.
            domStorageEnabled = true // Keeps its encrypted local identity in IndexedDB.
            databaseEnabled = false
            allowFileAccess = false
            allowContentAccess = false
            allowFileAccessFromFileURLs = false
            allowUniversalAccessFromFileURLs = false
            mixedContentMode = WebSettings.MIXED_CONTENT_NEVER_ALLOW
            setSupportMultipleWindows(false)
            userAgentString = "$userAgentString CrptochatAndroid/0.1"
        }
        WebView.setWebContentsDebuggingEnabled(BuildConfig.DEBUG)
        if (WebViewFeature.isFeatureSupported(WebViewFeature.SAFE_BROWSING_ENABLE)) {
            WebSettingsCompat.setSafeBrowsingEnabled(webView.settings, true)
        }
        // The web client reads crpto.url from localStorage before choosing its
        // default. Pin the configured WebSocket endpoint so a value saved during
        // another test cannot silently point this app at the wrong server.
        if (WebViewFeature.isFeatureSupported(WebViewFeature.DOCUMENT_START_SCRIPT)) {
            val script = "try{localStorage.setItem(" +
                JSONObject.quote("crpto.url") + "," +
                JSONObject.quote(BuildConfig.CHAT_WS_URL) + ")}catch(e){}"
            val port = if (origin.port != -1) ":${origin.port}" else ""
            // Rules are SCHEME://HOST[:PORT] (no path, no wildcard).
            val rule = "${origin.scheme}://${origin.host}$port"
            WebViewCompat.addDocumentStartJavaScript(webView, script, setOf(rule))
        }

        webView.webChromeClient = object : WebChromeClient() {
            override fun onPermissionRequest(request: PermissionRequest) {
                request.deny() // Camera, microphone and screen capture are outside this app's scope.
            }

            override fun onConsoleMessage(consoleMessage: ConsoleMessage): Boolean = false
        }
        webView.webViewClient = object : WebViewClient() {
            override fun shouldOverrideUrlLoading(view: WebView, request: WebResourceRequest): Boolean {
                return !isWebSocketRequest(request) && !isAllowed(request.url)
            }

            override fun shouldInterceptRequest(view: WebView, request: WebResourceRequest): WebResourceResponse? {
                // WebSocket handshakes are handled by WebView itself. Some WebView
                // versions surface them here; never replace them with a 403.
                if (isWebSocketRequest(request)) return null
                return if (isAllowed(request.url)) null else blockedResponse()
            }

            override fun onPageStarted(view: WebView, url: String, favicon: Bitmap?) {
                if (!isAllowed(Uri.parse(url))) view.stopLoading()
            }

            override fun onReceivedSslError(view: WebView, handler: SslErrorHandler, error: SslError) {
                handler.cancel() // Never allow users to bypass certificate errors.
            }
        }
    }

    private fun isWebSocketRequest(request: WebResourceRequest): Boolean {
        val scheme = request.url.scheme?.lowercase()
        if (scheme == "ws" || scheme == "wss") return true
        return request.requestHeaders.any { (name, value) ->
            name.equals("Upgrade", ignoreCase = true) && value.equals("websocket", ignoreCase = true)
        }
    }

    private fun isAllowed(url: Uri): Boolean {
        if (url.scheme != "https" && url.scheme != "http") return false
        val hostMatch = url.host.equals(origin.host, ignoreCase = true) ||
            (isLocalHost(url.host) && isLocalHost(origin.host))
        return hostMatch && normalizedPort(url) == normalizedPort(origin)
    }

    private fun isLocalHost(host: String?): Boolean {
        if (host == null) return false
        return host.equals("localhost", ignoreCase = true) || host == "127.0.0.1" || host == "10.0.2.2"
    }

    private fun normalizedPort(url: Uri): Int = when {
        url.port != -1 -> url.port
        url.scheme == "https" -> 443
        url.scheme == "http" -> 80
        else -> -1
    }

    private fun blockedResponse() = WebResourceResponse(
        "text/plain", "utf-8", 403, "Forbidden", emptyMap(), null
    )

    private fun showConfigurationError() {
        webView.visibility = View.GONE
        errorView.visibility = View.VISIBLE
        vpnBar.visibility = View.GONE
    }

    // ---- WireGuard VPN -------------------------------------------------------

    private fun onVpnButtonClicked() {
        if (vpnState == Tunnel.State.UP) {
            disconnectVpn()
            return
        }
        val prepareIntent = VpnService.prepare(this)
        if (prepareIntent != null) {
            Toast.makeText(this, R.string.vpn_permission_needed, Toast.LENGTH_SHORT).show()
            vpnPermissionLauncher.launch(prepareIntent)
        } else {
            openConfigPicker()
        }
    }

    private fun openConfigPicker() {
        Toast.makeText(this, R.string.vpn_select_config, Toast.LENGTH_SHORT).show()
        configPickerLauncher.launch("*/*")
    }

    private fun connectVpn(config: Config) {
        vpnState = Tunnel.State.TOGGLE
        updateVpnUi(vpnState)
        vpnExecutor.execute {
            try {
                val state = backend.setState(tunnel, Tunnel.State.UP, config)
                runOnUiThread { finishVpnChange(state, R.string.vpn_connected) }
            } catch (e: Exception) {
                runOnUiThread { showVpnFailure(e) }
            }
        }
    }

    private fun disconnectVpn() {
        vpnExecutor.execute {
            try {
                val state = backend.setState(tunnel, Tunnel.State.DOWN, null)
                runOnUiThread { finishVpnChange(state, R.string.vpn_disconnected) }
            } catch (e: Exception) {
                runOnUiThread { showVpnFailure(e) }
            }
        }
    }

    private fun finishVpnChange(state: Tunnel.State, messageRes: Int) {
        vpnState = state
        updateVpnUi(state)
        Toast.makeText(this, messageRes, Toast.LENGTH_SHORT).show()
    }

    private fun showVpnFailure(e: Exception) {
        vpnState = Tunnel.State.DOWN
        updateVpnUi(vpnState)
        Toast.makeText(
            this,
            getString(R.string.vpn_failed, e.message ?: e.javaClass.simpleName),
            Toast.LENGTH_LONG
        ).show()
    }

    private fun updateVpnUi(state: Tunnel.State) {
        vpnButton.setText(if (state == Tunnel.State.UP) R.string.vpn_disconnect else R.string.vpn_connect)
        vpnStatus.setText(
            when (state) {
                Tunnel.State.UP -> R.string.vpn_status_on
                Tunnel.State.TOGGLE -> R.string.vpn_status_connecting
                else -> R.string.vpn_status_off
            }
        )
    }

    override fun onDestroy() {
        vpnExecutor.shutdown()
        webView.destroy()
        super.onDestroy()
    }
}
