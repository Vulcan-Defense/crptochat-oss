package br.com.vulcan.cptochat

import android.annotation.SuppressLint
import android.graphics.Bitmap
import android.net.Uri
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
import android.widget.TextView
import androidx.activity.OnBackPressedCallback
import androidx.webkit.WebViewCompat
import androidx.appcompat.app.AppCompatActivity
import androidx.webkit.WebSettingsCompat
import androidx.webkit.WebViewFeature
import org.json.JSONObject

class MainActivity : AppCompatActivity() {
    private lateinit var webView: WebView
    private lateinit var errorView: TextView
    private lateinit var origin: Uri

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_main)
        webView = findViewById(R.id.chat_webview)
        errorView = findViewById(R.id.error_message)

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
    }

    override fun onDestroy() {
        webView.destroy()
        super.onDestroy()
    }
}
