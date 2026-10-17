package com.ava.app

import android.app.Activity
import android.content.Intent
import android.content.pm.PackageInfo
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Build
import android.provider.Settings
import androidx.core.content.FileProvider
import app.tauri.annotation.Command
import app.tauri.annotation.InvokeArg
import app.tauri.annotation.TauriPlugin
import app.tauri.plugin.Invoke
import app.tauri.plugin.JSObject
import app.tauri.plugin.Plugin
import java.io.File
import java.security.MessageDigest

@InvokeArg
class InstallUpdateArgs {
    lateinit var path: String
}

/**
 * Bridges a downloaded update file to the system installer.
 *
 * Rust has already matched the file against the published manifest digest.
 * This plugin adds the checks only the platform can make: the archive must
 * carry this app's package name and its exact signing certificate, which is
 * what makes "same signer" mean "same origin". The installer still asks the
 * user to confirm; a silent install is not available to side-loaded apps.
 */
@TauriPlugin
class AvaUpdatePlugin(private val activity: Activity) : Plugin(activity) {
    @Command
    fun info(invoke: Invoke) {
        val result = JSObject()
        result.put("versionCode", BuildConfig.VERSION_CODE.toLong())
        invoke.resolve(result)
    }

    @Command
    fun install(invoke: Invoke) {
        val path = try {
            invoke.parseArgs(InstallUpdateArgs::class.java).path
        } catch (_: Exception) {
            refuse(invoke, "request")
            return
        }
        val file = File(path)
        if (!file.isFile) {
            refuse(invoke, "missing")
            return
        }
        val manager = activity.packageManager
        val archive = archiveInfo(manager, path)
        if (archive == null || archive.packageName != activity.packageName) {
            refuse(invoke, "package")
            return
        }
        val installed = installedInfo(manager)
        if (installed == null || !sameSigner(installed, archive)) {
            refuse(invoke, "signature")
            return
        }
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O && !manager.canRequestPackageInstalls()) {
            try {
                activity.startActivity(
                    Intent(
                        Settings.ACTION_MANAGE_UNKNOWN_APP_SOURCES,
                        Uri.parse("package:${activity.packageName}"),
                    ),
                )
            } catch (_: Exception) {
                // The page still tells the user which setting to enable.
            }
            val result = JSObject()
            result.put("status", "permission-required")
            invoke.resolve(result)
            return
        }
        val uri = try {
            FileProvider.getUriForFile(activity, "${activity.packageName}.fileprovider", file)
        } catch (_: Exception) {
            refuse(invoke, "provider")
            return
        }
        val started = try {
            activity.startActivity(
                Intent(Intent.ACTION_VIEW)
                    .setDataAndType(uri, "application/vnd.android.package-archive")
                    .addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION),
            )
            true
        } catch (_: Exception) {
            false
        }
        if (!started) {
            refuse(invoke, "start")
            return
        }
        val result = JSObject()
        result.put("status", "installer-opened")
        invoke.resolve(result)
    }

    private fun refuse(invoke: Invoke, reason: String) {
        val result = JSObject()
        result.put("status", "rejected")
        result.put("reason", reason)
        invoke.resolve(result)
    }

    @Suppress("DEPRECATION")
    private fun archiveInfo(manager: PackageManager, path: String): PackageInfo? =
        try {
            manager.getPackageArchiveInfo(path, packageFlags())
        } catch (_: Exception) {
            null
        }

    @Suppress("DEPRECATION")
    private fun installedInfo(manager: PackageManager): PackageInfo? =
        try {
            manager.getPackageInfo(activity.packageName, packageFlags())
        } catch (_: Exception) {
            null
        }

    @Suppress("DEPRECATION")
    private fun packageFlags(): Int =
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.P) {
            PackageManager.GET_SIGNING_CERTIFICATES
        } else {
            PackageManager.GET_SIGNATURES
        }

    @Suppress("DEPRECATION")
    private fun signerDigests(info: PackageInfo): Set<String> {
        val signatures =
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.P) {
                info.signingInfo?.apkContentsSigners
            } else {
                info.signatures
            }
        return signatures?.map { signature -> sha256(signature.toByteArray()) }?.toSet().orEmpty()
    }

    private fun sameSigner(installed: PackageInfo, archive: PackageInfo): Boolean {
        val pinned = signerDigests(installed)
        return pinned.isNotEmpty() && pinned == signerDigests(archive)
    }

    private fun sha256(bytes: ByteArray): String =
        MessageDigest.getInstance("SHA-256")
            .digest(bytes)
            .joinToString("") { byte -> "%02x".format(byte.toInt() and 0xff) }
}
