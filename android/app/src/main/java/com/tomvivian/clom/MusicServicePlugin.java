package com.tomvivian.clom;

import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.content.IntentFilter;
import android.os.Build;
import com.getcapacitor.JSObject;
import com.getcapacitor.Plugin;
import com.getcapacitor.PluginCall;
import com.getcapacitor.PluginMethod;
import com.getcapacitor.annotation.CapacitorPlugin;

@CapacitorPlugin(name = "MusicServicePlugin")
public class MusicServicePlugin extends Plugin {

    private BroadcastReceiver mediaActionReceiver;

    @Override
    public void load() {
        mediaActionReceiver = new BroadcastReceiver() {
            @Override
            public void onReceive(Context context, Intent intent) {
                String action = intent.getStringExtra("action");
                if (action != null) {
                    JSObject data = new JSObject();
                    data.put("action", action);
                    notifyListeners("mediaAction", data);
                }
            }
        };
        IntentFilter filter = new IntentFilter("com.tomvivian.clom.MEDIA_ACTION");
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            getContext().registerReceiver(mediaActionReceiver, filter, Context.RECEIVER_NOT_EXPORTED);
        } else {
            getContext().registerReceiver(mediaActionReceiver, filter);
        }
    }

    @PluginMethod
    public void start(PluginCall call) {
        String title = call.getString("title", "Clom");
        String artist = call.getString("artist", "");
        String coverUri = call.getString("coverUri", "");
        Intent intent = new Intent(getContext(), MusicService.class);
        intent.putExtra("title", title);
        intent.putExtra("artist", artist);
        if (coverUri != null && !coverUri.isEmpty()) intent.putExtra("coverUri", coverUri);
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            getContext().startForegroundService(intent);
        } else {
            getContext().startService(intent);
        }
        call.resolve();
    }

    @PluginMethod
    public void update(PluginCall call) {
        String title = call.getString("title", "Clom");
        String artist = call.getString("artist", "");
        String playState = call.getString("playState", "playing");
        String coverUri = call.getString("coverUri", "");
        Intent intent = new Intent(getContext(), MusicService.class);
        intent.setAction("UPDATE");
        intent.putExtra("title", title);
        intent.putExtra("artist", artist);
        intent.putExtra("playState", playState);
        if (coverUri != null && !coverUri.isEmpty()) intent.putExtra("coverUri", coverUri);
        getContext().startService(intent);
        call.resolve();
    }

    @PluginMethod
    public void stop(PluginCall call) {
        Intent intent = new Intent(getContext(), MusicService.class);
        intent.setAction("STOP");
        try {
            getContext().startService(intent);
        } catch (Exception e) {
            // Service might not be running
        }
        call.resolve();
    }

    @Override
    protected void handleOnDestroy() {
        if (mediaActionReceiver != null) {
            try {
                getContext().unregisterReceiver(mediaActionReceiver);
            } catch (Exception e) {
                // Already unregistered
            }
        }
    }
}
