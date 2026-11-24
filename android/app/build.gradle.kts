plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

android {
    namespace = "br.com.vulcan.cptochat"
    compileSdk = 36

    defaultConfig {
        applicationId = "br.com.vulcan.cptochat"
        minSdk = 26
        targetSdk = 36
        versionCode = 1
        versionName = "0.1.0"

        // Production defaults: the web client lives under /web/ and derives
        // wss://<host>/ws itself. Override both together for local testing, e.g.
        // -PchatOrigin=http://localhost:8123/ -PchatWsUrl=ws://localhost:7778
        // (localhost requires `adb reverse tcp:8123 tcp:8123` and tcp:7778).
        val chatOrigin = providers.gradleProperty("chatOrigin")
            .orElse("https://crptochat.acpt.app.br/web/")
            .get()
        val chatWsUrl = providers.gradleProperty("chatWsUrl")
            .orElse("wss://crptochat.acpt.app.br/ws")
            .get()
        buildConfigField("String", "CHAT_ORIGIN", "\"$chatOrigin\"")
        buildConfigField("String", "CHAT_WS_URL", "\"$chatWsUrl\"")
    }

    buildFeatures {
        buildConfig = true
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }

    kotlinOptions {
        jvmTarget = "17"
    }
}

dependencies {
    implementation("androidx.appcompat:appcompat:1.7.0")
    implementation("androidx.webkit:webkit:1.12.1")
    // Userspace WireGuard tunnel; its AAR declares the VpnService for us.
    implementation("com.wireguard.android:tunnel:1.0.20250531")
}
