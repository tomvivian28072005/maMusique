package com.tomvivian.clom;

import android.app.Activity;
import android.content.Intent;
import android.database.Cursor;
import android.net.Uri;
import android.os.Build;
import android.provider.DocumentsContract;
import android.util.Base64;

import androidx.core.content.FileProvider;

import androidx.activity.result.ActivityResult;

import com.getcapacitor.JSArray;
import com.getcapacitor.JSObject;
import com.getcapacitor.Plugin;
import com.getcapacitor.PluginCall;
import com.getcapacitor.PluginMethod;
import com.getcapacitor.annotation.ActivityCallback;
import com.getcapacitor.annotation.CapacitorPlugin;

import java.io.ByteArrayOutputStream;
import java.io.InputStream;

@CapacitorPlugin(name = "FolderPicker")
public class FolderPickerPlugin extends Plugin {

    private static final String[] AUDIO_EXTENSIONS = {
        ".mp3", ".ogg", ".flac", ".m4a", ".aac", ".wav", ".opus", ".wma"
    };

    @PluginMethod
    public void pickFolder(PluginCall call) {
        Intent intent = new Intent(Intent.ACTION_OPEN_DOCUMENT_TREE);
        intent.addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION);
        startActivityForResult(call, intent, "folderPickerResult");
    }

    @ActivityCallback
    private void folderPickerResult(PluginCall call, ActivityResult result) {
        if (call == null) return;
        if (result.getResultCode() != Activity.RESULT_OK || result.getData() == null) {
            call.resolve(new JSObject().put("files", new JSArray()));
            return;
        }

        Uri treeUri = result.getData().getData();
        if (treeUri == null) {
            call.resolve(new JSObject().put("files", new JSArray()));
            return;
        }

        // Get the document ID for the selected tree
        String docId = DocumentsContract.getTreeDocumentId(treeUri);
        Uri childrenUri = DocumentsContract.buildChildDocumentsUriUsingTree(treeUri, docId);

        JSArray filesArray = new JSArray();

        try (Cursor cursor = getContext().getContentResolver().query(
                childrenUri,
                new String[]{
                    DocumentsContract.Document.COLUMN_DOCUMENT_ID,
                    DocumentsContract.Document.COLUMN_DISPLAY_NAME,
                    DocumentsContract.Document.COLUMN_MIME_TYPE,
                    DocumentsContract.Document.COLUMN_SIZE
                },
                null, null, null)) {

            if (cursor != null) {
                while (cursor.moveToNext()) {
                    String name = cursor.getString(1);
                    String mime = cursor.getString(2);
                    if (name == null) continue;

                    // Check if it's an audio file
                    boolean isAudio = false;
                    if (mime != null && mime.startsWith("audio/")) {
                        isAudio = true;
                    } else {
                        String lowerName = name.toLowerCase();
                        for (String ext : AUDIO_EXTENSIONS) {
                            if (lowerName.endsWith(ext)) { isAudio = true; break; }
                        }
                    }
                    if (!isAudio) continue;

                    String childDocId = cursor.getString(0);
                    Uri fileUri = DocumentsContract.buildDocumentUriUsingTree(treeUri, childDocId);

                    JSObject fileObj = new JSObject();
                    fileObj.put("name", name);
                    fileObj.put("uri", fileUri.toString());
                    fileObj.put("size", cursor.getLong(3));
                    filesArray.put(fileObj);
                }
            }
        } catch (Exception e) {
            call.reject("Error reading folder: " + e.getMessage());
            return;
        }

        JSObject ret = new JSObject();
        ret.put("files", filesArray);
        call.resolve(ret);
    }

    @PluginMethod
    public void readFileBase64(PluginCall call) {
        String uriStr = call.getString("uri");
        if (uriStr == null) { call.reject("Missing uri"); return; }

        try {
            Uri uri = Uri.parse(uriStr);
            InputStream is = getContext().getContentResolver().openInputStream(uri);
            if (is == null) { call.reject("Cannot open file"); return; }

            ByteArrayOutputStream baos = new ByteArrayOutputStream();
            byte[] buf = new byte[8192];
            int len;
            while ((len = is.read(buf)) != -1) {
                baos.write(buf, 0, len);
            }
            is.close();

            String base64 = Base64.encodeToString(baos.toByteArray(), Base64.NO_WRAP);
            JSObject ret = new JSObject();
            ret.put("base64", base64);
            call.resolve(ret);
        } catch (Exception e) {
            call.reject("Error reading file: " + e.getMessage());
        }
    }

    @PluginMethod
    public void getClomFolderPath(PluginCall call) {
        try {
            java.io.File extDir = getContext().getExternalFilesDir(null);
            if (extDir != null) {
                java.io.File clomDir = new java.io.File(extDir, "Clom/downloads");
                if (!clomDir.exists()) clomDir.mkdirs();
                JSObject ret = new JSObject();
                ret.put("path", clomDir.getAbsolutePath());
                call.resolve(ret);
            } else {
                call.reject("External storage not available");
            }
        } catch (Exception e) {
            call.reject(e.getMessage());
        }
    }

    @PluginMethod
    public void openFileManager(PluginCall call) {
        try {
            java.io.File extDir = getContext().getExternalFilesDir(null);
            java.io.File clomDir = new java.io.File(extDir, "Clom/downloads");
            if (!clomDir.exists()) clomDir.mkdirs();

            // Try to open the folder with the file manager
            Uri uri = Uri.parse("content://com.android.externalstorage.documents/document/primary%3AAndroid%2Fdata%2F" + getContext().getPackageName() + "%2Ffiles%2FClom%2Fdownloads");
            Intent intent = new Intent(Intent.ACTION_VIEW);
            intent.setDataAndType(uri, "vnd.android.document/directory");
            intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
            try {
                getActivity().startActivity(intent);
                call.resolve();
                return;
            } catch (android.content.ActivityNotFoundException ignored) {}

            // Fallback: open generic file manager
            Intent fallback = new Intent("android.provider.action.BROWSE");
            fallback.setData(Uri.parse("content://com.android.externalstorage.documents/root/primary"));
            fallback.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
            try {
                getActivity().startActivity(fallback);
                call.resolve();
                return;
            } catch (android.content.ActivityNotFoundException ignored2) {}

            // Last fallback: open any file manager app
            Intent generic = new Intent(Intent.ACTION_VIEW);
            generic.setData(Uri.parse("file://" + clomDir.getAbsolutePath()));
            generic.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
            try {
                getActivity().startActivity(generic);
                call.resolve();
            } catch (android.content.ActivityNotFoundException e) {
                JSObject ret = new JSObject();
                ret.put("error", "no_file_manager");
                ret.put("path", clomDir.getAbsolutePath());
                call.resolve(ret);
            }
        } catch (Exception e) {
            call.reject(e.getMessage());
        }
    }

    @PluginMethod
    public void downloadFile(PluginCall call) {
        String url = call.getString("url");
        String destPath = call.getString("path");
        if (url == null || destPath == null) { call.reject("Missing url or path"); return; }

        // Run on background thread
        new Thread(() -> {
            try {
                java.net.URL fileUrl = new java.net.URL(url);
                java.net.HttpURLConnection conn = (java.net.HttpURLConnection) fileUrl.openConnection();
                conn.setInstanceFollowRedirects(true);
                conn.setRequestProperty("User-Agent", "Clom-App");
                conn.connect();

                // Follow redirects manually for HTTPS→HTTPS
                int code = conn.getResponseCode();
                while (code == 301 || code == 302 || code == 303 || code == 307) {
                    String loc = conn.getHeaderField("Location");
                    conn.disconnect();
                    conn = (java.net.HttpURLConnection) new java.net.URL(loc).openConnection();
                    conn.setInstanceFollowRedirects(true);
                    conn.setRequestProperty("User-Agent", "Clom-App");
                    conn.connect();
                    code = conn.getResponseCode();
                }

                if (code != 200) { call.reject("HTTP " + code); return; }

                java.io.File dest = new java.io.File(destPath);
                dest.getParentFile().mkdirs();

                InputStream is = conn.getInputStream();
                java.io.FileOutputStream fos = new java.io.FileOutputStream(dest);
                byte[] buf = new byte[8192];
                int len;
                while ((len = is.read(buf)) != -1) {
                    fos.write(buf, 0, len);
                }
                fos.close();
                is.close();
                conn.disconnect();

                JSObject ret = new JSObject();
                ret.put("path", dest.getAbsolutePath());
                call.resolve(ret);
            } catch (Exception e) {
                call.reject("Download failed: " + e.getMessage());
            }
        }).start();
    }

    @PluginMethod
    public void installApk(PluginCall call) {
        String filePath = call.getString("path");
        if (filePath == null) { call.reject("Missing path"); return; }

        try {
            java.io.File apkFile = new java.io.File(filePath);
            if (!apkFile.exists()) { call.reject("APK file not found"); return; }

            Uri apkUri = FileProvider.getUriForFile(
                getContext(),
                getContext().getPackageName() + ".fileprovider",
                apkFile
            );

            Intent intent = new Intent(Intent.ACTION_VIEW);
            intent.setDataAndType(apkUri, "application/vnd.android.package-archive");
            intent.addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION);
            intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
            getContext().startActivity(intent);
            call.resolve();
        } catch (Exception e) {
            call.reject("Install failed: " + e.getMessage());
        }
    }
}
