plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

android {
    namespace = "ai.karry.observer"
    compileSdk = 35

    defaultConfig {
        applicationId = "ai.karry.observer"
        minSdk = 26
        targetSdk = 35
        versionCode = 1
        versionName = "0.1.0"

        testInstrumentationRunner = "androidx.test.runner.AndroidJUnitRunner"
    }

    flavorDimensions += "agent"
    productFlavors {
        create("agent1") {
            dimension = "agent"
            resValue("string", "app_name", "Karry-Agent 1")
            buildConfigField("String", "DEFAULT_OBSERVER_ID", "\"observer-android\"")
            buildConfigField("String", "DEFAULT_OPERATION_ID", "\"operation-iphone\"")
        }
        create("agent3") {
            dimension = "agent"
            applicationIdSuffix = ".agent3"
            resValue("string", "app_name", "Karry-Agent 3")
            buildConfigField("String", "DEFAULT_OBSERVER_ID", "\"observer-android-3\"")
            buildConfigField("String", "DEFAULT_OPERATION_ID", "\"operation-iphone-4\"")
        }
    }

    buildTypes {
        release {
            isMinifyEnabled = true
            proguardFiles(
                getDefaultProguardFile("proguard-android-optimize.txt"),
                "proguard-rules.pro",
            )
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    kotlinOptions {
        jvmTarget = "17"
    }
    buildFeatures {
        buildConfig = true
    }
}

dependencies {
    implementation("androidx.appcompat:appcompat:1.7.0")
    implementation("androidx.core:core-ktx:1.15.0")
    implementation("androidx.lifecycle:lifecycle-runtime-ktx:2.8.7")
    implementation("com.google.android.material:material:1.12.0")
    implementation("org.jetbrains.kotlinx:kotlinx-coroutines-android:1.9.0")

    testImplementation("junit:junit:4.13.2")
}
