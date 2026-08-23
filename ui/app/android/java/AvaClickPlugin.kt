package com.ava.shell

import android.app.Activity
import android.content.Intent
import android.webkit.WebView
import app.tauri.annotation.Command
import app.tauri.annotation.TauriPlugin
import app.tauri.plugin.Invoke
import app.tauri.plugin.JSObject
import app.tauri.plugin.Plugin

/**
 * Captures notification taps because the upstream notification plugin's event
 * can be lost before JavaScript starts on a cold launch. The tap intent is the
 * only reliable signal, so the bridge consumes this mailbox once it is ready.
 */
@TauriPlugin
class AvaClickPlugin(private val activity: Activity) : Plugin(activity) {
    private var pendingClick: Boolean = false

    override fun load(webView: WebView) {
        super.load(webView)
        capture(activity.intent)
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        capture(intent)
    }

    private fun capture(intent: Intent) {
        if (
            intent.action == Intent.ACTION_MAIN &&
                intent.getIntExtra("NotificationId", Int.MIN_VALUE) != Int.MIN_VALUE
        ) {
            pendingClick = true
        }
    }

    @Command
    fun takePendingClick(invoke: Invoke) {
        val result = JSObject()
        result.put("pending", pendingClick)
        pendingClick = false
        invoke.resolve(result)
    }
}
