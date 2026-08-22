package com.ava.shell

import android.app.Activity
import app.tauri.annotation.Command
import app.tauri.annotation.InvokeArg
import app.tauri.annotation.TauriPlugin
import app.tauri.plugin.Invoke
import app.tauri.plugin.JSObject
import app.tauri.plugin.Plugin

@InvokeArg
class SaveSecretArgs {
    lateinit var secret: String
}

/** Minimal native bridge for the Keystore-backed cluster secret. */
@TauriPlugin
class AvaSecretPlugin(activity: Activity) : Plugin(activity) {
    private val store = AvaSecretStore(activity.applicationContext)

    @Command
    fun save(invoke: Invoke) {
        try {
            store.save(invoke.parseArgs(SaveSecretArgs::class.java).secret)
            invoke.resolve()
        } catch (_: Exception) {
            invoke.reject("could not save the cluster secret")
        }
    }

    @Command
    fun get(invoke: Invoke) {
        val result = JSObject()
        result.put("secret", store.get())
        invoke.resolve(result)
    }

    @Command
    fun clear(invoke: Invoke) {
        store.clear()
        invoke.resolve()
    }
}
