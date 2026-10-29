import java.util.Properties

plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
    id("com.google.devtools.ksp")
}

fun prop(name: String, default: String = ""): String =
    (project.findProperty(name) as String?)?.trim() ?: default

fun csv(value: String): List<String> =
    value.split(',').map { it.trim() }.filter { it.isNotEmpty() }

// Keystore de DESENVOLVIMENTO (gerada por scripts/gen-dev-keystore.sh; diretório ignorado).
// Release real = keystore do proprietário (fora do repositório), informada pelas mesmas chaves.
val devKeystoreProps = Properties().apply {
    val f = rootProject.file("keystore/dev.properties")
    if (f.isFile) f.inputStream().use { load(it) }
}

android {
    namespace = "br.com.rdmidiaindoor.player"
    compileSdk = 35
    buildToolsVersion = "35.0.0"

    defaultConfig {
        applicationId = "br.com.rdmidiaindoor.player"
        minSdk = 24
        targetSdk = 35
        versionCode = 1
        versionName = "1.0.0"
        buildConfigField("String", "DEFAULT_API_BASE_URL", "\"${prop("rd.apiBaseUrl")}\"")
        buildConfigField("String", "UPDATE_PUBLIC_KEYS", "\"${prop("rd.update.publicKeys")}\"")
        buildConfigField("String", "PROTOCOL", "\"RDPP/1\"")
    }

    signingConfigs {
        if (devKeystoreProps.getProperty("storeFile") != null) {
            create("dev") {
                storeFile = rootProject.file(devKeystoreProps.getProperty("storeFile"))
                storePassword = devKeystoreProps.getProperty("storePassword")
                keyAlias = devKeystoreProps.getProperty("keyAlias")
                keyPassword = devKeystoreProps.getProperty("keyPassword")
            }
        }
    }

    buildTypes {
        getByName("debug") {
            buildConfigField("String", "CLEARTEXT_HOSTS", "\"${csv(prop("rd.dev.cleartextHosts")).joinToString(",")}\"")
            buildConfigField("boolean", "KIOSK_BLOCK_ADB", "false")
            // A debug.keystore padrão do container é efêmera (nova a cada `docker run`): usa a keystore de
            // desenvolvimento quando existir, para que builds debug sucessivos se atualizem com `adb install -r`.
            signingConfigs.findByName("dev")?.let { signingConfig = it }
        }
        getByName("release") {
            isMinifyEnabled = true
            isShrinkResources = true
            proguardFiles(getDefaultProguardFile("proguard-android-optimize.txt"), "proguard-rules.pro")
            buildConfigField("String", "CLEARTEXT_HOSTS", "\"${csv(prop("rd.release.cleartextHosts")).joinToString(",")}\"")
            buildConfigField("boolean", "KIOSK_BLOCK_ADB", "true")
            signingConfig = signingConfigs.findByName("dev")
        }
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

    testOptions {
        unitTests.all {
            it.systemProperty("rd.vectors.dir", rootProject.file("../protocol/vectors").absolutePath)
            it.testLogging {
                events("passed", "skipped", "failed")
                showStandardStreams = false
                exceptionFormat = org.gradle.api.tasks.testing.logging.TestExceptionFormat.FULL
            }
        }
    }

    lint {
        abortOnError = true
        checkReleaseBuilds = true
        warningsAsErrors = false
        htmlReport = true
        xmlReport = true
        textReport = true
    }

    packaging {
        resources.excludes += setOf("META-INF/AL2.0", "META-INF/LGPL2.1", "META-INF/*.kotlin_module")
    }
}

// network_security_config gerado por variante: HTTPS por padrão; HTTP só para hosts de desenvolvimento
// configurados explicitamente (gradle.properties: rd.dev.cleartextHosts / rd.release.cleartextHosts).
abstract class GenerateNetworkSecurityConfig : DefaultTask() {
    @get:Input
    abstract val cleartextHosts: ListProperty<String>

    @get:OutputDirectory
    abstract val outputDir: DirectoryProperty

    @TaskAction
    fun generate() {
        val dir = outputDir.get().asFile.resolve("xml")
        dir.mkdirs()
        val hosts = cleartextHosts.get()
        val domainConfig = if (hosts.isEmpty()) "" else buildString {
            append("    <domain-config cleartextTrafficPermitted=\"true\">\n")
            hosts.forEach { append("        <domain includeSubdomains=\"false\">").append(it).append("</domain>\n") }
            append("    </domain-config>\n")
        }
        dir.resolve("network_security_config.xml").writeText(
            """<?xml version="1.0" encoding="utf-8"?>
<!-- GERADO pelo build (app/build.gradle.kts). Não editar. -->
<network-security-config>
    <base-config cleartextTrafficPermitted="false">
        <trust-anchors>
            <certificates src="system" />
        </trust-anchors>
    </base-config>
$domainConfig</network-security-config>
"""
        )
    }
}

androidComponents {
    onVariants { variant ->
        val hosts = if (variant.buildType == "debug") csv(prop("rd.dev.cleartextHosts")) else csv(prop("rd.release.cleartextHosts"))
        val taskName = "generate${variant.name.replaceFirstChar { it.uppercase() }}NetworkSecurityConfig"
        val task = tasks.register<GenerateNetworkSecurityConfig>(taskName) {
            cleartextHosts.set(hosts)
        }
        variant.sources.res?.addGeneratedSourceDirectory(task, GenerateNetworkSecurityConfig::outputDir)
    }
}

dependencies {
    implementation("androidx.core:core-ktx:1.13.1")
    implementation("org.jetbrains.kotlinx:kotlinx-coroutines-android:1.8.1")
    implementation("androidx.media3:media3-exoplayer:1.4.1")
    implementation("androidx.media3:media3-ui:1.4.1")
    implementation("androidx.work:work-runtime-ktx:2.9.1")
    implementation("androidx.room:room-runtime:2.6.1")
    implementation("androidx.room:room-ktx:2.6.1")
    ksp("androidx.room:room-compiler:2.6.1")
    implementation("com.squareup.okhttp3:okhttp:4.12.0")
    implementation("com.google.zxing:core:3.5.3")

    testImplementation("junit:junit:4.13.2")
    testImplementation("org.jetbrains.kotlinx:kotlinx-coroutines-test:1.8.1")
    testImplementation("com.squareup.okhttp3:mockwebserver:4.12.0")
}
