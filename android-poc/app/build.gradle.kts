plugins {
    id("com.android.application")
    id("com.chaquo.python")
}

val repositoryRoot = rootProject.projectDir.parentFile

val stageGoodOldPhotosPython by tasks.registering(Sync::class) {
    from(repositoryRoot) {
        include(
            "app.py",
            "auth.py",
            "backup_engine.py",
            "database.py",
            "album_routes.py",
            "worker_notify.py",
            "google_photos.py",
            "download_manager.py",
            "storage.py",
            "github.py",
        )
    }
    into(layout.buildDirectory.dir("generated/chaquopy/goodoldphotos"))
}

val stageGoodOldPhotosWebAssets by tasks.registering(Sync::class) {
    from(repositoryRoot.resolve("templates")) {
        into("templates")
    }
    from(repositoryRoot.resolve("static")) {
        into("static")
    }
    into(layout.buildDirectory.dir("generated/assets/goodoldphotos"))
}

chaquopy {
    sourceSets {
        getByName("main") {
            srcDir(stageGoodOldPhotosPython)
        }
    }
}

android {
    namespace = "org.goodoldphotos.androidpoc"
    compileSdk = 35

    sourceSets.getByName("main").assets.srcDir(stageGoodOldPhotosWebAssets)

    defaultConfig {
        applicationId = "org.goodoldphotos.androidpoc"
        minSdk = 24
        targetSdk = 35
        versionCode = 1
        versionName = "1.0"

        ndk {
            abiFilters += listOf("arm64-v8a")
        }
    }

    testOptions {
        unitTests.isReturnDefaultValues = true
    }
}

dependencies {
    testImplementation("junit:junit:4.13.2")
}

chaquopy {
    defaultConfig {
        version = "3.13"
        buildPython("/home/anmol/.local/share/chaquopy/bin/python3.13")

        pip {
            install("-r", "requirements-android.txt")
        }
    }
}

tasks.named("preBuild") {
    dependsOn(stageGoodOldPhotosPython, stageGoodOldPhotosWebAssets)
}
