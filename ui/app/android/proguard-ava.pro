# Rust calls PluginManager/PluginHandle by JNI name and discovers commands
# reflectively from @Command. R8 must not rename or strip them: release sets
# isMinifyEnabled=true.
-keep class com.ava.app.** { *; }
-keep class app.tauri.plugin.PluginManager { public *; }
-keep class app.tauri.plugin.PluginHandle { public *; }
-keep class app.tauri.annotation.** { *; }
-keep class app.tauri.Rust { *; }
-keepattributes *Annotation*
