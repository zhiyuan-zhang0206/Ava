package com.ava.app

import android.app.Activity
import android.webkit.CookieManager
import android.webkit.ValueCallback
import app.tauri.annotation.Command
import app.tauri.annotation.InvokeArg
import app.tauri.annotation.TauriPlugin
import app.tauri.plugin.Invoke
import app.tauri.plugin.Plugin

@InvokeArg
class SetCookieArgs {
    lateinit var url: String
    lateinit var cookie: String
}

/** Installs a native-login session cookie into Android WebView's cookie store. */
@TauriPlugin
class AvaCookiePlugin(activity: Activity) : Plugin(activity) {
    @Command
    fun set(invoke: Invoke) {
        val args = try {
            invoke.parseArgs(SetCookieArgs::class.java)
        } catch (_: Exception) {
            invoke.reject("could not install the session cookie")
            return
        }
        val cookieManager = try {
            CookieManager.getInstance()
        } catch (_: Exception) {
            invoke.reject("could not install the session cookie")
            return
        }
        try {
            cookieManager.setCookie(
                args.url,
                args.cookie,
                ValueCallback { accepted ->
                    if (accepted != true) {
                        invoke.reject("could not install the session cookie")
                    } else {
                        try {
                            cookieManager.flush()
                            invoke.resolve()
                        } catch (_: Exception) {
                            invoke.reject("could not install the session cookie")
                        }
                    }
                },
            )
        } catch (_: Exception) {
            invoke.reject("could not install the session cookie")
        }
    }
}
