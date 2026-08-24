package com.ava.app

import android.app.Activity
import android.content.Intent
import android.os.Build
import app.tauri.annotation.Command
import app.tauri.annotation.TauriPlugin
import app.tauri.plugin.Invoke
import app.tauri.plugin.Plugin

/**
 * The Rust side's handle on [AvaBackgroundService].
 *
 * Registered from `src/android.rs` via `register_android_plugin`, which
 * instantiates this class by name with the activity and loads it into Tauri's
 * PluginManager. Commands are discovered reflectively from the `@Command`
 * annotation, so no annotation processor is involved.
 */
@TauriPlugin
class AvaBackgroundPlugin(private val activity: Activity) : Plugin(activity) {

    @Command
    fun startService(invoke: Invoke) {
        val intent = Intent(activity, AvaBackgroundService::class.java)
        // Oreo and later refuse a plain startService for a background start;
        // startForegroundService promises a foreground notification instead.
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            activity.startForegroundService(intent)
        } else {
            activity.startService(intent)
        }
        invoke.resolve()
    }

    @Command
    fun stopService(invoke: Invoke) {
        activity.stopService(Intent(activity, AvaBackgroundService::class.java))
        invoke.resolve()
    }
}
