package com.ava.shell

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.os.Build
import android.os.IBinder
import androidx.core.app.NotificationCompat

/**
 * Keeps the shell's process — and therefore its webview, and therefore the SSE
 * connection the notification bridge rides on — alive while the app is in the
 * background.
 *
 * Android reaps backgrounded processes at will, and a reaped process silently
 * stops delivering notifications. A foreground service is the supported way to
 * opt out, and the persistent notification below is the price Android charges
 * for it. The user can turn the whole thing off in the shell's settings, which
 * stops this service.
 */
class AvaBackgroundService : Service() {

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        startForeground(NOTIFICATION_ID, buildNotification())
        // Restart if the system kills us under memory pressure — staying
        // connected is the entire purpose of this service.
        return START_STICKY
    }

    private fun buildNotification(): Notification {
        createChannel()

        // Tapping the persistent notification returns to the console rather
        // than starting a second task.
        val launch = packageManager.getLaunchIntentForPackage(packageName)?.apply {
            flags = Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_CLEAR_TOP
        }
        val pendingFlags = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M) {
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
        } else {
            PendingIntent.FLAG_UPDATE_CURRENT
        }
        val contentIntent = launch?.let {
            PendingIntent.getActivity(this, 0, it, pendingFlags)
        }

        return NotificationCompat.Builder(this, CHANNEL_ID)
            .setContentTitle("Ava")
            .setContentText("Connected — watching your agents")
            .setSmallIcon(android.R.drawable.stat_notify_sync)
            .setOngoing(true)
            // Low priority: this notification is a permission slip, not news.
            .setPriority(NotificationCompat.PRIORITY_LOW)
            .setContentIntent(contentIntent)
            .build()
    }

    private fun createChannel() {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.O) return
        val manager = getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
        if (manager.getNotificationChannel(CHANNEL_ID) != null) return
        val channel = NotificationChannel(
            CHANNEL_ID,
            "Background connection",
            NotificationManager.IMPORTANCE_LOW,
        )
        channel.description = "Keeps Ava connected so agent notifications arrive."
        channel.setShowBadge(false)
        manager.createNotificationChannel(channel)
    }

    companion object {
        private const val CHANNEL_ID = "ava_background"
        private const val NOTIFICATION_ID = 1
    }
}
