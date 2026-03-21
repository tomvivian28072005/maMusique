package com.tomvivian.clom;

import com.getcapacitor.BridgeActivity;
import android.os.Bundle;

public class MainActivity extends BridgeActivity {
    @Override
    public void onCreate(Bundle savedInstanceState) {
        registerPlugin(YtExtractorPlugin.class);
        registerPlugin(MusicServicePlugin.class);
        super.onCreate(savedInstanceState);
    }
}
