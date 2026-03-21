package com.tomvivian.clom;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.Intent;
import android.graphics.Bitmap;
import android.graphics.BitmapFactory;
import android.net.Uri;
import android.os.Build;
import android.os.IBinder;
import android.support.v4.media.MediaMetadataCompat;
import android.support.v4.media.session.MediaSessionCompat;
import android.support.v4.media.session.PlaybackStateCompat;
import androidx.core.app.NotificationCompat;
import androidx.media.app.NotificationCompat.MediaStyle;
import androidx.media.session.MediaButtonReceiver;
import java.io.InputStream;

public class MusicService extends Service {
    private static final String CHANNEL_ID = "clom_music";
    private static final int NOTIFICATION_ID = 1;
    private String currentTitle = "Clom";
    private String currentArtist = "Lecture en cours";
    private String currentCoverUri = null;
    private Bitmap currentCoverBitmap = null;
    private boolean isPlaying = true;
    private MediaSessionCompat mediaSession;

    @Override
    public void onCreate() {
        super.onCreate();
        createNotificationChannel();
        initMediaSession();
    }

    private void initMediaSession() {
        mediaSession = new MediaSessionCompat(this, "ClomMediaSession");
        mediaSession.setFlags(
            MediaSessionCompat.FLAG_HANDLES_MEDIA_BUTTONS |
            MediaSessionCompat.FLAG_HANDLES_TRANSPORT_CONTROLS
        );
        mediaSession.setCallback(new MediaSessionCompat.Callback() {
            @Override
            public void onPlay() {
                isPlaying = true;
                updatePlaybackState();
                updateNotification();
                sendActionToWebView("play");
            }
            @Override
            public void onPause() {
                isPlaying = false;
                updatePlaybackState();
                updateNotification();
                sendActionToWebView("pause");
            }
            @Override
            public void onSkipToNext() {
                sendActionToWebView("next");
            }
            @Override
            public void onSkipToPrevious() {
                sendActionToWebView("prev");
            }
            @Override
            public void onStop() {
                isPlaying = false;
                updatePlaybackState();
                stopForeground(true);
                stopSelf();
            }
        });
        mediaSession.setActive(true);
        updatePlaybackState();
    }

    private void sendActionToWebView(String action) {
        Intent intent = new Intent("com.tomvivian.clom.MEDIA_ACTION");
        intent.putExtra("action", action);
        sendBroadcast(intent);
    }

    private void updatePlaybackState() {
        long actions = PlaybackStateCompat.ACTION_PLAY |
            PlaybackStateCompat.ACTION_PAUSE |
            PlaybackStateCompat.ACTION_SKIP_TO_NEXT |
            PlaybackStateCompat.ACTION_SKIP_TO_PREVIOUS |
            PlaybackStateCompat.ACTION_STOP |
            PlaybackStateCompat.ACTION_PLAY_PAUSE;

        int state = isPlaying ?
            PlaybackStateCompat.STATE_PLAYING :
            PlaybackStateCompat.STATE_PAUSED;

        PlaybackStateCompat playbackState = new PlaybackStateCompat.Builder()
            .setActions(actions)
            .setState(state, PlaybackStateCompat.PLAYBACK_POSITION_UNKNOWN, 1.0f)
            .build();
        mediaSession.setPlaybackState(playbackState);
    }

    private void loadCoverBitmap(String coverUri) {
        if (coverUri == null || coverUri.isEmpty()) {
            currentCoverUri = null;
            currentCoverBitmap = null;
            return;
        }
        if (coverUri.equals(currentCoverUri) && currentCoverBitmap != null) return;
        currentCoverUri = coverUri;
        try {
            InputStream is = getContentResolver().openInputStream(Uri.parse(coverUri));
            if (is != null) {
                BitmapFactory.Options opts = new BitmapFactory.Options();
                opts.inSampleSize = 2; // Réduire pour économiser la mémoire
                currentCoverBitmap = BitmapFactory.decodeStream(is, null, opts);
                is.close();
            }
        } catch (Exception e) {
            currentCoverBitmap = null;
        }
    }

    private void updateMediaMetadata() {
        MediaMetadataCompat.Builder builder = new MediaMetadataCompat.Builder()
            .putString(MediaMetadataCompat.METADATA_KEY_TITLE, currentTitle)
            .putString(MediaMetadataCompat.METADATA_KEY_ARTIST, currentArtist)
            .putString(MediaMetadataCompat.METADATA_KEY_ALBUM, "Clom");
        if (currentCoverBitmap != null) {
            builder.putBitmap(MediaMetadataCompat.METADATA_KEY_ART, currentCoverBitmap);
            builder.putBitmap(MediaMetadataCompat.METADATA_KEY_ALBUM_ART, currentCoverBitmap);
        }
        mediaSession.setMetadata(builder.build());
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        // Route media button intents to the media session
        MediaButtonReceiver.handleIntent(mediaSession, intent);
        if (intent != null) {
            String action = intent.getAction();
            if ("UPDATE".equals(action)) {
                currentTitle = intent.getStringExtra("title");
                currentArtist = intent.getStringExtra("artist");
                if (currentTitle == null) currentTitle = "Clom";
                if (currentArtist == null) currentArtist = "";
                String playState = intent.getStringExtra("playState");
                if ("paused".equals(playState)) isPlaying = false;
                else if ("playing".equals(playState)) isPlaying = true;
                String coverUri = intent.getStringExtra("coverUri");
                if (coverUri != null) loadCoverBitmap(coverUri);
                updateMediaMetadata();
                updatePlaybackState();
                updateNotification();
                return START_STICKY;
            } else if ("STOP".equals(action)) {
                isPlaying = false;
                updatePlaybackState();
                mediaSession.setActive(false);
                stopForeground(true);
                stopSelf();
                return START_NOT_STICKY;
            }
        }
        // Initial start
        if (intent != null) {
            currentTitle = intent.getStringExtra("title");
            currentArtist = intent.getStringExtra("artist");
            if (currentTitle == null) currentTitle = "Clom";
            if (currentArtist == null) currentArtist = "";
            String coverUri = intent.getStringExtra("coverUri");
            if (coverUri != null) loadCoverBitmap(coverUri);
        }
        isPlaying = true;
        mediaSession.setActive(true);
        updateMediaMetadata();
        updatePlaybackState();
        startForeground(NOTIFICATION_ID, buildNotification());
        return START_STICKY;
    }

    private void createNotificationChannel() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            NotificationChannel channel = new NotificationChannel(
                CHANNEL_ID, "Lecture audio", NotificationManager.IMPORTANCE_LOW);
            channel.setDescription("Notification de lecture audio Clom");
            channel.setShowBadge(false);
            NotificationManager nm = getSystemService(NotificationManager.class);
            if (nm != null) nm.createNotificationChannel(channel);
        }
    }

    private Notification buildNotification() {
        Intent launchIntent = new Intent(this, MainActivity.class);
        launchIntent.setFlags(Intent.FLAG_ACTIVITY_SINGLE_TOP);
        PendingIntent pi = PendingIntent.getActivity(this, 0, launchIntent,
            PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);

        NotificationCompat.Builder builder = new NotificationCompat.Builder(this, CHANNEL_ID)
            .setContentTitle(currentTitle)
            .setContentText(currentArtist)
            .setSmallIcon(android.R.drawable.ic_media_play)
            .setContentIntent(pi)
            .setOngoing(isPlaying)
            .setSilent(true)
            .setVisibility(NotificationCompat.VISIBILITY_PUBLIC)
            .setStyle(new MediaStyle()
                .setMediaSession(mediaSession.getSessionToken())
                .setShowActionsInCompactView(0, 1, 2))
            .addAction(android.R.drawable.ic_media_previous, "Précédent",
                createMediaAction(PlaybackStateCompat.ACTION_SKIP_TO_PREVIOUS))
            .addAction(isPlaying ?
                android.R.drawable.ic_media_pause :
                android.R.drawable.ic_media_play,
                isPlaying ? "Pause" : "Lecture",
                createMediaAction(isPlaying ?
                    PlaybackStateCompat.ACTION_PAUSE :
                    PlaybackStateCompat.ACTION_PLAY))
            .addAction(android.R.drawable.ic_media_next, "Suivant",
                createMediaAction(PlaybackStateCompat.ACTION_SKIP_TO_NEXT));

        if (currentCoverBitmap != null) {
            builder.setLargeIcon(currentCoverBitmap);
        }

        return builder.build();
    }

    private PendingIntent createMediaAction(long action) {
        int keyCode;
        switch ((int)action) {
            case (int)PlaybackStateCompat.ACTION_SKIP_TO_PREVIOUS:
                keyCode = android.view.KeyEvent.KEYCODE_MEDIA_PREVIOUS;
                break;
            case (int)PlaybackStateCompat.ACTION_PAUSE:
                keyCode = android.view.KeyEvent.KEYCODE_MEDIA_PAUSE;
                break;
            case (int)PlaybackStateCompat.ACTION_PLAY:
                keyCode = android.view.KeyEvent.KEYCODE_MEDIA_PLAY;
                break;
            case (int)PlaybackStateCompat.ACTION_SKIP_TO_NEXT:
                keyCode = android.view.KeyEvent.KEYCODE_MEDIA_NEXT;
                break;
            default:
                keyCode = android.view.KeyEvent.KEYCODE_MEDIA_PLAY_PAUSE;
        }
        Intent intent = new Intent(Intent.ACTION_MEDIA_BUTTON);
        intent.setPackage(getPackageName());
        intent.putExtra(Intent.EXTRA_KEY_EVENT,
            new android.view.KeyEvent(android.view.KeyEvent.ACTION_DOWN, keyCode));
        return PendingIntent.getBroadcast(this, keyCode, intent,
            PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);
    }

    private void updateNotification() {
        NotificationManager nm = getSystemService(NotificationManager.class);
        if (nm != null) nm.notify(NOTIFICATION_ID, buildNotification());
    }

    @Override
    public void onDestroy() {
        if (mediaSession != null) {
            mediaSession.setActive(false);
            mediaSession.release();
        }
        super.onDestroy();
    }

    @Override
    public IBinder onBind(Intent intent) {
        return null;
    }
}
